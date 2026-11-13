"""Frozen five-paper target-yield ablation; never connects to tumor_api."""
import argparse
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from defusedxml import ElementTree

from apps.curation.decision import digest
from apps.curation.knowledge_relation_prompt import DEFINITION
from scripts.prepare_knowledge_intake import freeze, read, sha
from scripts.prepare_passage_pilot import load_verified_version, node_text
from scripts.run_minimal_pipeline import prepare_entities, relation_inputs

BASE = ROOT / 'var/experiments/target-yield-20261106'
EXPERIMENTS = ROOT / 'var/experiments'
LIBRARY = Path('/appdata/02_resource/apps/biomed/production/var/library')
PMIDS = ['34096690', '40445863', '42872145', '42872089', '42584173']


def source_document(pmid):
    """Bounded indexed reads; verify actual immutable JATS, not just metadata."""
    db = sqlite3.connect('file:/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        queue = sqlite3.connect('file:/appdata/02_resource/apps/biomed/operations/knowledge-processing/queue.sqlite3?mode=ro', uri=True, timeout=3)
        try:
            source = json.loads(queue.execute('SELECT source FROM papers WHERE pmid=? LIMIT 1', (pmid,)).fetchone()[0])
        finally:
            queue.close()
        version = dict(db.execute('SELECT * FROM curation_materialversion WHERE id=?', (source['version_id'],)).fetchone())
        material = dict(db.execute('SELECT * FROM curation_material WHERE id=?', (source['material_id'],)).fetchone())
        passages = [dict(r) for r in db.execute('SELECT id,version_id,parser_version,ordinal,locator,text FROM curation_passage WHERE version_id=? AND parser_version=? ORDER BY ordinal LIMIT 1025',
                                               (source['version_id'], 'text-v1-pypdf-6.20.0-jats-1'))]
    finally:
        db.close()
    if len(passages) > 1024:
        raise ValueError('Source exceeds bounded read')
    try:
        path, raw = load_verified_version(LIBRARY, version)
    except PermissionError:
        # Read through the existing service mount; never chmod production blobs.
        checksum = version['sha256']
        if version['integrity_status'] != 'valid' or len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
            raise ValueError('Unverified blob identity') from None
        path = LIBRARY / 'blobs' / checksum[:2] / checksum
        raw = subprocess.check_output(['docker', 'exec', 'biomed-web', 'cat', f'/app/library/blobs/{checksum[:2]}/{checksum}'], timeout=30)
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError('Source checksum mismatch') from None
    root = ElementTree.fromstring(raw)
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    article = root if root.tag == 'article' else root.find('.//article')
    ids = {n.attrib.get('pub-id-type'): node_text(n) for n in article.findall('front/article-meta/article-id')}
    if ids.get('pmid') != pmid:
        raise ValueError('JATS PMID mismatch')
    parsed = {}
    for section in ('front/article-meta/abstract', 'body'):
        area = article.find(section)
        if area is not None:
            parsed.update({f'{section}/p:{i}': node_text(n) for i, n in enumerate(area.iter('p'), 1) if node_text(n)})
    for p in passages:
        if parsed.get(p['locator']) != p['text']:
            raise ValueError('Exact source locator mismatch')
        p.update(pmid=pmid, source_sha256=version['sha256'])
    info = {'pmid': pmid, 'pmcid': ids.get('pmc') or ids.get('pmcid'), 'doi': ids.get('doi'),
            'title': material['title'], 'material_id': material['id'], 'version_id': version['id'],
            'source_sha256': version['sha256'], **{k: version[k] for k in ('source_url', 'rights', 'acquired_at')}}
    return {'article': info, 'passages': passages, 'blob': str(path), 'verified_sha256': hashlib.sha256(raw).hexdigest()}


def packet(doc, locators):
    passages = []
    for i, locator in enumerate(locators):
        p = copy.deepcopy(next(p for p in doc['passages'] if p['locator'] == locator))
        p.update(alias=f'P{i+1}', role='anchor' if i == 0 else 'support')
        passages.append(p)
    result = {'id': f"Y-{doc['article']['pmid']}-0000", 'article_pmid': doc['article']['pmid'],
              'article': doc['article'], 'anchor_id': passages[0]['id'], 'passages': passages,
              'context_characters': sum(len(p['text']) for p in passages),
              'context_budget_characters': 12000, 'context_budget_status': 'within_budget',
              'selection': 'Preselected molecular-clinical development sample, not an unseen screening evaluation'}
    result['input_sha256'] = digest(result)
    return result


def prepare(base):
    controls = read(EXPERIMENTS / 'target-optimization-20261106/relations-v4/packets.json')
    legacy_path = EXPERIMENTS / 'ten-paper-closure-20261102/stages/entities/assembled-packets.json'
    legacy = read(legacy_path)
    borrowed = controls['records'] + [next(r for r in legacy['records'] if r['id'] == 'E-42584173-0000')]
    new = []
    for pmid, locators in [('42872145', ['body/p:22', 'body/p:9', 'front/article-meta/abstract/p:1']),
                           ('42872089', ['body/p:10', 'front/article-meta/abstract/p:1'])]:
        doc = source_document(pmid)
        freeze(base / f'sources/{pmid}.json', doc)
        new.append(packet(doc, locators))
    freeze(base / 'borrowed-entity-packets.json', {'schema_version': controls['schema_version'], 'records': borrowed})
    freeze(base / 'new-source-packets.json', {'schema_version': controls['schema_version'], 'records': new})
    prepare_entities(base / 'new-source-packets.json', base / 'stages')
    for name, path in {
        'dictionary.json': EXPERIMENTS / 'target-compatibility-20261101/target-dictionary.json',
        'registry.json': EXPERIMENTS / 'ten-paper-closure-20261102/registry-snapshot.json',
    }.items():
        freeze(base / name, read(path))
    freeze(base / 'selection.json', {'version': 'target-yield-five-v1', 'pmids': PMIDS,
        'control_pmids': PMIDS[:2], 'new_coverage_pmids': PMIDS[2:],
        'selection_reason': 'Source confirms clinical disease relationship plus a specific variant, rearrangement or trial molecular eligibility. FGFR3 pooled alteration scope retained as a difficult case, not replaced after scoring.',
        'fixed_packets': [r['id'] for r in borrowed + new],
        'borrowed_entities': [{'packet': r['id'], 'entity_stage': r['entity_stage']} for r in borrowed],
        'baseline': 'Current v4 relation prompt + v6 automatic mapping on these SAME five inputs.',
        'comparison': 'Paired v4/v5 prompt and baseline/clinical adapter ablation; unique article/relationship counts, controls excluded from new yield.',
        'limits': 'Purposefully selected development sample; no whole-corpus precision or recall estimate. Existing fulltext and 3 exact prior 9B inventories reused; 2 new 9B inventories. No downloads, model changes, training or target writes.',
        'legacy_entity_source_sha256': sha(legacy_path), 'target_contacted': False, 'target_written': False})
    print('Frozen 5 papers; 2 new entity packets, 3 verified prior inventories')


def prepare_relations(base):
    existing = read(base / 'borrowed-entity-packets.json')
    new = read(base / 'stages/entities/assembled-packets.json')
    dataset = {**existing, 'records': existing['records'] + new['records']}
    packets, inputs, deferred = relation_inputs(dataset)
    if deferred or len(inputs['records']) != 5:
        raise ValueError('All five fixed packets must have located model entity inventories')
    for name in ('baseline', 'focused'):
        definition = copy.deepcopy(DEFINITION)
        if name == 'focused':
            definition['version'] = 'target-clinical-focus-v1'
            definition['system_prompt'] += '''\nFor this molecular clinical evidence task, retain a molecular diagnostic relationship when ANCHOR explicitly establishes one, even without a drug or numeric endpoint. Use biomarker_association for a diagnostic molecular finding, explicitly name the diagnosis in population and state the diagnostic finding in results. This is not automatically a treatment claim. Distinguish actually detected genomic changes from immunostaining, prior treatment, VUS, and background. Preserve tested gene/variant spelling and numbering; do not reconcile conflicting spellings, guess fusion partners, expand broad alteration groups into individual mutations, or infer sensitivity from co-occurrence. A case molecular diagnosis must remain a case-level observation, not a sufficient universal diagnostic rule. If a statement lacks sufficient molecular specificity, preserve the broad wording; scripts will keep it out of the target-ready set. Do not add extra facts just to increase the count.'''
        folder = base / name
        freeze(folder / 'packets.json', packets)
        freeze(folder / 'all-packets.json', dataset)
        freeze(folder / 'inputs.json', inputs)
        freeze(folder / 'definition.json', definition)
    print('Frozen paired relation inputs: baseline v4 and clinical focus, same models and five inventories')


def evaluate(base):
    from apps.curation.knowledge_clinical_mapping import adapt, load_gene_alias_standard
    from apps.curation.knowledge_export import export_bundle
    from apps.curation.knowledge_target_mapping import prepare_mappings
    from apps.curation.minimal_knowledge import build_bundle
    from scripts.minimal_knowledge_pilot import standard_linker

    dictionary, registry = read(base / 'dictionary.json'), read(base / 'registry.json')
    standard = load_gene_alias_standard(ROOT / 'data/knowledge/genes.jsonl')
    translations_path = Path('/appdata/02_resource/apps/biomed/resources/cancer-20261025/translations.json')
    translations = read(translations_path)
    linker = standard_linker()
    arms, products = [], {}
    for name in ('baseline', 'focused'):
        folder = base / name
        packets = read(folder / 'packets.json')['records']
        results = {p['id']: read(folder / 'extract-gpu0' / f"result-{p['id']}.json") for p in packets}
        bundle, assembly = build_bundle(packets, results, source_run=f'target-yield-{name}-20261106', mode='inventory', linker=linker)
        for adapter in ('legacy', 'clinical'):
            start = time.perf_counter()
            if adapter == 'legacy':
                mappings, report = prepare_mappings(bundle, dictionary, registry, gene_standard=standard)
                package = export_bundle(bundle, dictionary=dictionary, mappings=mappings)
            else:
                mappings, package, report = adapt(bundle, dictionary, registry, standard, translations)
            seconds = time.perf_counter() - start
            label = name + '-' + adapter
            products[label] = {'bundle': bundle, 'assembly': assembly, 'mappings': mappings, 'package': package, 'report': report}
            ready = [r for r in package['records'] if r['offline_ready']]
            facts = {r['group_id']: r for r in bundle['records']}
            # One source article / target relationship, not a count of endpoint
            # wording variants or repeated paragraphs. Subgroups remain in the
            # reviewed facts; this key never silently rewrites their payload.
            def identity(row, facts=facts):
                return digest([facts[row['group_id']]['article']['pmid'], row['target_payload']['evidence_category'], row['target_payload']['conclusion_inputs']])
            unique = {identity(row): row for row in ready}
            arms.append({'id': label, 'records': len(bundle['records']), 'offline_ready_records': len(ready),
                         'unique_ready': len(unique), 'ready_groups': [r['group_id'] for r in unique.values()],
                         'new_coverage_ready': sum(facts[r['group_id']]['article']['pmid'] not in PMIDS[:2] for r in unique.values()),
                         'adapter_seconds': round(seconds, 6),
                         'relation_inference_seconds': read(folder / 'extract-gpu0/summary.json')['total_inference_seconds']})
    # Diagnostics are replaceable work products; final frozen deliveries are
    # explicitly written after source review, never overwritten by this command.
    from scripts.classify_literature import atomic_json
    atomic_json(base / 'evaluation-working.json', {'arms': arms, 'products': products,
                'translation_sha256': sha(translations_path), 'gene_standard_source': standard['source']})
    print(json.dumps(arms, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'relations', 'evaluate'])
    parser.add_argument('--root', type=Path, default=BASE)
    args = parser.parse_args()
    {'prepare': prepare, 'relations': prepare_relations, 'evaluate': evaluate}[args.command](args.root)
