"""Verify and freeze Astra's independent review of the three new articles."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PMIDS = {'35664754', '41059188', '35941871'}
JUDGMENTS = {'supported', 'partial', 'unsupported', 'uncertain'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(root, review_path):
    root, review_path = Path(root), Path(review_path)
    raw = review_path.read_bytes()
    review = json.loads(raw)
    for name in ('source-only.json', 'astra-source-reference.json', 'new-candidates-comparison.json'):
        if review.get('input_sha256', {}).get(name) != sha(root / name):
            raise ValueError(f'Review input differs: {name}')
    if review.get('model') != 'gpt-6-astra':
        raise ValueError('Review model identity differs')
    source = json.loads((root/'source-only.json').read_text())
    comparison = json.loads((root/'new-candidates-comparison.json').read_text())
    articles = {str(article['pmid']): article for article in source['articles'] if str(article['pmid']) in PMIDS}
    if set(articles) != PMIDS:
        raise ValueError('Source articles differ')
    passages = {pmid: {p['id']: p for p in article['passages']} for pmid, article in articles.items()}
    expected = {(str(row['pmid']), row['group_id']) for row in comparison['records']
                if row['baseline_offline_ready']
                or row['treatment_preview']['status'] == 'inactive_structure_preview'
                or row['diagnostic_preview']['status'] == 'inactive_structure_preview'}
    records, omissions = review.get('records'), review.get('omissions')
    if not isinstance(records, list) or not isinstance(omissions, list):
        raise TypeError('Review needs records and omissions arrays')
    seen, judgments, facts, citations = set(), Counter(), set(), 0

    def evidence(row, pmid):
        nonlocal citations
        entries = row.get('evidence')
        if not isinstance(entries, list) or not entries:
            raise ValueError('Each reviewed fact needs source evidence')
        for item in entries:
            passage = passages[pmid].get(item.get('passage_id'))
            quote = item.get('quote')
            if (not passage or passage['locator'] != item.get('locator')
                    or not isinstance(quote, str) or not quote or quote not in passage['text']):
                raise ValueError('Astra quotation differs from the cited source')
            citations += 1

    for row in records:
        pmid, group, fact = str(row.get('pmid')), row.get('group_id'), row.get('independent_fact_id')
        identity = (pmid, group)
        if (identity not in expected or not isinstance(fact, str) or not fact.strip()
                or (identity, fact) in seen or row.get('core_source_fact_judgment') not in JUDGMENTS
                or row.get('target_structure_judgment') not in JUDGMENTS):
            raise ValueError('Review contains an unexpected or duplicate candidate judgment')
        evidence(row, pmid)
        seen.add((identity, fact))
        facts.add((pmid, fact))
        judgments[row['core_source_fact_judgment']] += 1
    if {identity for identity, _ in seen} != expected:
        raise ValueError('Review does not cover every candidate that passed a structural method')
    omitted = set()
    for row in omissions:
        pmid, fact = str(row.get('pmid')), row.get('independent_fact_id')
        if pmid not in PMIDS or not isinstance(fact, str) or not fact.strip() or (pmid, fact) in facts:
            raise ValueError('Omission must be a distinct source fact, not an extracted fact')
        evidence(row, pmid)
        omitted.add((pmid, fact))
    validation = {'version': 'fusion-independent-astra-review-integrity-v1',
                  'articles': len(PMIDS), 'structure_candidate_groups': len(expected),
                  'reviewed_records': len(records), 'distinct_reviewed_facts': len(facts),
                  'distinct_omitted_facts': len(omitted), 'core_judgments': dict(judgments),
                  'source_quotes_verified': citations, 'review_sha256': hashlib.sha256(raw).hexdigest(),
                  'reference_kind': 'independent_model_not_clinical_gold',
                  'target_written': False, 'created_at': datetime.now(timezone.utc).isoformat()}
    destination, result = root/'astra-new-review.json', root/'astra-new-review-validation.json'
    if destination.exists():
        if destination.read_bytes() != raw:
            raise FileExistsError('A different frozen Astra review already exists')
    else:
        with destination.open('xb') as stream:
            stream.write(raw)
    if result.exists():
        existing = json.loads(result.read_text())
        if any(existing.get(key) != value for key, value in validation.items() if key != 'created_at'):
            raise FileExistsError('A different validation result already exists')
        return existing
    with result.open('x', encoding='utf-8') as stream:
        json.dump(validation, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return validation


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.review), ensure_ascii=False))
