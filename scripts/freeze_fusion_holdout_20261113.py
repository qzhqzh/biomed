"""Freeze five independent fusion-related full texts without writing production.

Three targeted PMC full texts are acquired only while the existing collector
is inactive, serially, at its 2-second/256-KiB/s limits and PMC off-peak hours.
Two challenge articles reuse verified, previously processed source snapshots.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from apps.curation.classification import taxonomy_hash, taxonomy_snapshot
from apps.curation.collection_network import pmc_off_peak_delay
from apps.curation.decision import digest
from apps.curation.evidence_pilot import (
    copy_sources_via_worker,
    prepare_document,
    read_source,
)
from apps.curation.materials import PARSER_VERSION, strip_namespaces, validate_pmc
from apps.curation.network import fetch, text, xml
from scripts.freeze_target_holdout_20261113 import LICENSE, excluded_pmids
from scripts.prepare_knowledge_intake import freeze

DATABASE = Path('/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3')
LIBRARY = Path('/appdata/02_resource/apps/biomed/production/var/library')
OPS = Path('/appdata/02_resource/apps/biomed/operations/knowledge-processing')
PREVIOUS = ROOT / 'var/experiments/target-holdout-20261113-v1/selection.json'
CASES = [
    {'pmid': '35664754', 'pmcid': 'PMC9162556', 'stratum': 'single_case_treatment_signal'},
    {'pmid': '41059188', 'pmcid': 'PMC12500244', 'stratum': 'single_case_sequential_therapies'},
    {'pmid': '35941871', 'pmcid': 'PMC9356229', 'stratum': 'multiple_cases_challenge'},
    {'pmid': '42834928', 'batch': 'kp-20261108T083743-4f637e7e', 'stratum': 'diagnostic_challenge'},
    {'pmid': '42828699', 'batch': 'kp-20261108T173652-f4ffff2f', 'stratum': 'preclinical_challenge'},
]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def existing_collection_active():
    with sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5) as db:
        db.execute('PRAGMA query_only=ON')
        return bool(db.execute("SELECT 1 FROM curation_literaturecollection WHERE state IN ('running','estimating') LIMIT 1").fetchone())


def target_metadata():
    with sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        rows = []
        for case in CASES:
            if not case.get('pmcid'):
                continue
            values = db.execute('SELECT id,created_at,identifiers FROM curation_material WHERE source_key=?',
                                ('pubmed:' + case['pmid'],)).fetchall()
            if len(values) != 1:
                raise ValueError('Targeted PubMed metadata missing or ambiguous')
            row = dict(values[0])
            identifiers = json.loads(row['identifiers'])
            if str(identifiers.get('pubmed')) != case['pmid'] or str(identifiers.get('pmc', '')).upper() != case['pmcid']:
                raise ValueError('Stored PubMed and PMC identifiers differ')
            rows.append({'created_at': row['created_at'], 'material_id': row['id'], 'pmid': case['pmid'],
                         'pmcid': case['pmcid'], 'identifiers': identifiers})
    return rows


def source_document(case, bundle, library, taxonomy, output, rank):
    doc = prepare_document(bundle, rank, library, {}, taxonomy)
    if doc['pmid'] != case['pmid'] or not doc['model_input']:
        raise ValueError('Verified metadata unavailable')
    if doc['fulltext_version_id']:
        raise ValueError('New full text now exists in production; reuse it instead of downloading again')
    download = output / 'downloads' / (case['pmcid'] + '.xml')
    download.parent.mkdir(parents=True, exist_ok=True)
    url = 'https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/?' + urlencode({
        'verb': 'GetRecord', 'identifier': 'oai:pubmedcentral.nih.gov:' + case['pmcid'][3:], 'metadataPrefix': 'pmc'})
    if download.exists():
        content = download.read_bytes()
    else:
        if existing_collection_active():
            raise RuntimeError('Collector active: defer research download to avoid exceeding its combined limit')
        if pmc_off_peak_delay(datetime.now(timezone.utc)):
            raise RuntimeError('PMC bulk off-peak window closed; retain current checkpoint')
        content = fetch(url, limit=25 * 1024 * 1024, bytes_per_second=256 * 1024, interval=2)
        # Retain the response for resumption, including unsupported licensing.
        with download.open('xb') as stream:
            stream.write(content)
    rights = validate_pmc(content, case['pmcid'], case['identifiers'])
    if not LICENSE.search(rights):
        raise ValueError(f"{case['pmcid']} has no explicit CC BY 4.0 or CC0 license")
    sha = hashlib.sha256(content).hexdigest()
    blob = library / 'blobs' / sha[:2] / sha
    blob.parent.mkdir(parents=True, exist_ok=True)
    if blob.exists() and hashlib.sha256(blob.read_bytes()).hexdigest() != sha:
        raise ValueError('Research source blob differs from its digest')
    if not blob.exists():
        with blob.open('xb') as stream:
            stream.write(content)
    version_id = uuid5(NAMESPACE_URL, f"biomed-research:{case['pmcid']}:{sha}").hex
    version = {'id': version_id, 'material_id': doc['material_id'], 'sha256': sha,
               'media_type': 'application/jats+xml', 'filename': case['pmcid'] + '.xml',
               'source_url': url, 'rights': rights, 'integrity_status': 'valid', 'integrity_note': '',
               'acquired_at': datetime.now(timezone.utc).isoformat()}
    article = strip_namespaces(xml(content)).find('.//article')
    passages = []
    for section in ('front/article-meta/abstract', 'body'):
        area = article.find(section)
        if area is None:
            continue
        for number, paragraph in enumerate(area.iter('p'), 1):
            value = text(paragraph)
            if value:
                locator = f'{section}/p:{number}'
                passages.append({'id': uuid5(NAMESPACE_URL, f'{version_id}:{locator}').hex,
                                 'version_id': version_id, 'parser_version': PARSER_VERSION,
                                 'ordinal': len(passages), 'locator': locator, 'text': value, 'kind': 'fulltext'})
    if not passages or sum(len(p['text']) for p in passages) < 80:
        raise ValueError('Full text produced insufficient paragraphs')
    doc.update(versions=[*doc['versions'], version], fulltext_version_id=version_id, passages=passages,
               passages_sha256=digest(passages), input_scope='parsed_fulltext',
               source_issues=[v for v in doc['source_issues'] if v != 'parsed_fulltext_missing'])
    return doc


def run(output):
    output = Path(output)
    previous = {str(x['pmid']) for x in read(PREVIOUS)} | excluded_pmids()
    if previous.intersection(case['pmid'] for case in CASES):
        raise ValueError('Independent source selection overlaps earlier development articles')
    output.mkdir(parents=True, exist_ok=True)
    selection = {'version': 'fusion-independent-articles-v1', 'articles': CASES,
                 'selection_method': 'Two single-case treatment signals, one multi-case challenge, '
                                     'and two previously processed diagnostic/preclinical challenges; not a random sample.',
                 'article_labels_are_not_reference': True, 'production_written': False, 'target_written': False}
    freeze(output / 'selection.json', selection)
    metadata = target_metadata()
    freeze(output / 'metadata-selection.json', {'selection_method': 'five-article targeted research selection',
                                                'records': metadata})
    bundles, selection_info = read_source(DATABASE, len(metadata), selection_file=output / 'metadata-selection.json')
    library = copy_sources_via_worker(bundles, output)
    taxonomy = taxonomy_snapshot()
    by_pmid = {str(b['material']['identifiers'].get('pubmed')): b for b in bundles}
    docs = [source_document(c, by_pmid[c['pmid']], library, taxonomy, output, rank)
            for rank, c in enumerate(metadata, 1)]
    snapshot = {'version': 'evidence-pilot-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                'database': str(DATABASE), 'library': str(LIBRARY), 'readable_library': str(library),
                'limit': len(docs), 'selection': selection_info, 'taxonomy': taxonomy,
                'taxonomy_sha256': taxonomy_hash(taxonomy), 'exposure_check': {'status': 'known_article_exclusions'},
                'documents': docs}
    freeze(output / 'new-source-snapshot.json', {'sha256': digest(snapshot), 'snapshot': snapshot})
    freeze(output / 'new-pmids.json', {'pmids': [c['pmid'] for c in metadata]})
    sources = {d['pmid']: d for d in docs}
    for case in CASES:
        if not case.get('batch'):
            continue
        path = OPS / 'batches' / case['batch'] / 'sources/snapshot.json'
        envelope = read(path)
        if envelope['sha256'] != digest(envelope['snapshot']):
            raise ValueError('Existing batch source snapshot checksum differs')
        matching = [d for d in envelope['snapshot']['documents'] if str(d.get('pmid')) == case['pmid']]
        if len(matching) != 1:
            raise ValueError('Existing challenge source unavailable or ambiguous')
        doc = matching[0]
        version = next(v for v in doc['versions'] if v['id'] == doc['fulltext_version_id'])
        if not LICENSE.search(str(version.get('rights') or '')):
            raise ValueError('Existing challenge source is not explicitly CC BY 4.0 or CC0')
        sources[case['pmid']] = doc
    articles = []
    for case in CASES:
        doc = sources[case['pmid']]
        version = next(v for v in doc['versions'] if v['id'] == doc['fulltext_version_id'])
        if digest(doc['passages']) != doc['passages_sha256']:
            raise ValueError('Source passage checksum differs')
        articles.append({'pmid': case['pmid'], 'title': doc['title'], 'stratum': case['stratum'],
                         'version_id': version['id'], 'source_sha256': version['sha256'], 'rights': version['rights'],
                         'passages': [{'id': p['id'], 'locator': p['locator'], 'text': p['text']}
                                      for p in doc['passages'] if p.get('text')]})
    reference = {'version': 'fusion-independent-source-only-v1', 'articles': articles,
                 'candidate_outputs_included': False, 'labels_are_sampling_hints_only': True}
    freeze(output / 'source-only.json', reference)
    freeze(output / 'manifest.json', {'version': 'fusion-independent-freeze-v1', 'articles': len(articles),
           'downloaded_fulltexts': len(metadata), 'reused_existing_fulltexts': len(articles) - len(metadata),
           'source_sha256': digest(reference), 'new_snapshot_sha256': digest(snapshot),
           'licensing': 'Explicit CC BY 4.0 or CC0', 'production_written': False, 'target_written': False})
    return {'articles': len(articles), 'new_fulltexts': len(metadata), 'reused_fulltexts': len(articles)-len(metadata),
            'passages': sum(len(a['passages']) for a in articles), 'production_written': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False))
