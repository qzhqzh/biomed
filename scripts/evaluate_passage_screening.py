#!/usr/bin/env python3
"""Offline paragraph screening metrics; never access the collection database."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.curation.decision import digest
from apps.curation.pilot_candidates import locate_candidates


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def metrics(rows, reference, predictions):
    counts = Counter()
    missed, false_positives, pending = [], [], []
    for row in rows:
        key = row['id']
        actual, predicted = reference[key]['relevant'], predictions[key]['relevant']
        if actual is None:
            counts['unscored'] += 1
            continue
        counts['denominator'] += 1
        counts['positive' if actual else 'negative'] += 1
        if predicted is None:
            pending.append(key)
        if predicted is True:
            counts['selected'] += 1
            if actual:
                counts['tp'] += 1
            else:
                counts['fp'] += 1
                false_positives.append(key)
        elif actual:
            counts['fn'] += 1
            missed.append(key)
        elif predicted is False:
            counts['tn'] += 1
    result = {key: counts[key] for key in ('denominator', 'unscored', 'positive', 'negative', 'selected', 'tp', 'fp', 'fn', 'tn')}
    tp, fp, fn = counts['tp'], counts['fp'], counts['fn']
    result.update(precision=tp / (tp + fp) if tp + fp else None,
                  recall=tp / (tp + fn) if tp + fn else None,
                  f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
                  missed=missed, false_positives=false_positives, pending=pending,
                  selected_all=sum(predictions[r['id']]['relevant'] is True for r in rows),
                  pending_all=sum(predictions[r['id']]['relevant'] is None for r in rows),
                  total=len(rows))
    return result


def validate_reference(rows, reference, definition_sha):
    assert reference['annotator_model'] == 'gpt-6-astra'
    assert reference['blinded_to_candidates'] is True or (
        reference.get('initial_annotation_blinded') is True
        and reference.get('parent_reference_sha256')
        and reference.get('review_artifact')
    )
    assert reference['definition_sha256'] == definition_sha
    assert reference['input_sha256'] == digest(rows)
    refs = {r['id']: r for r in reference['records']}
    assert len(refs) == len(reference['records']) == len(rows)
    assert set(refs) == {r['id'] for r in rows}
    for row in rows:
        ref = refs[row['id']]
        assert ref['relevant'] is None or type(ref['relevant']) is bool
        assert ref['input_sha256'] == row['input_sha256'] and ref['pmid'] == row['pmid']
        assert ref['reason'].strip()
        if ref['relevant'] is True:
            assert ref['evidence']
        for quote in ref.get('evidence', []):
            assert type(quote['start']) is int and type(quote['end']) is int
            assert 0 <= quote['start'] < quote['end'] <= len(row['text'])
            assert row['text'][quote['start']:quote['end']] == quote['quote']
    return refs


def load_run(folder, rows, definition, input_hash):
    manifest, summary = read(folder / 'manifest.json'), read(folder / 'summary.json')
    assert manifest['input_sha256'] == input_hash
    assert manifest['config_sha256'] == digest(manifest['config'])
    assert summary['state'] == 'completed' and summary['processed'] == len(rows)
    assert manifest['config']['definition'] == definition
    results = {}
    for row in rows:
        result = read(folder / f"result-{row['id']}.json")
        for key in ('id', 'pmid', 'input_sha256'):
            assert result[key] == row[key]
        assert result['config_sha256'] == manifest['config_sha256']
        assert result['relevant'] is None or type(result['relevant']) is bool
        if result['status'] == 'classified':
            assert type(result['relevant']) is bool
        else:
            assert result['relevant'] is None
        assert result['seconds'] >= 0
        results[row['id']] = result
    return results, summary


def evaluate(base, output, reference_path=None):
    if output.exists():
        raise ValueError('Existing evaluation is immutable; use a new --output path')
    reference_path = (reference_path or base / 'astra-reference-v1.json').resolve()
    assert reference_path.parent == base.resolve()
    inputs = read(base / 'inputs.json')
    rows = inputs['records']
    assert len({r['id'] for r in rows}) == len(rows)
    assert len({r['pmid'] for r in rows}) == 5
    for row in rows:
        assert digest({k: v for k, v in row.items() if k != 'input_sha256'}) == row['input_sha256']
    definition = read(base / 'definition.json')
    reference = read(reference_path)
    refs = validate_reference(rows, reference, sha(base / 'definition.json'))
    if reference.get('parent_reference_sha256'):
        parent_path = base / reference['parent_reference_artifact']
        review_path = base / reference['review_artifact']
        assert sha(parent_path) == reference['parent_reference_sha256']
        assert sha(review_path) == reference['review_sha256']
        parent = validate_reference(rows, read(parent_path), sha(base / 'definition.json'))
        review = read(review_path)
        assert review['annotator_model'] == 'gpt-6-astra'
        assert review['definition_sha256'] == sha(base / 'definition.json')
        changes = {r['id']: r for r in review['records'] if r['decision'] == 'revise'}
        assert {key for key in refs if refs[key] != parent[key]} == set(changes)
        for key, change in changes.items():
            assert parent[key]['relevant'] is change['previous_relevant']
            assert refs[key]['relevant'] is change['recommended_relevant']
            assert refs[key]['evidence'] == change['evidence']
            assert refs[key]['reason'] == change['reason']
    methods, timings = {}, {}
    begin = time.perf_counter()
    candidates = locate_candidates({'passages': [{'id': r['id'], 'kind': 'fulltext', 'locator': r['locator'], 'text': r['text']} for r in rows]}, max_candidates=len(rows))
    rule_seconds = time.perf_counter() - begin
    rule_ids = {r['passage_id'] for r in candidates['candidates']}
    for candidate in candidates['candidates']:
        row = next(r for r in rows if r['id'] == candidate['passage_id'])
        assert row['text'][candidate['start']:candidate['end']] == candidate['quote']
    methods['rules'] = {r['id']: {'relevant': r['id'] in rule_ids} for r in rows}
    timings['rules'] = {'seconds': rule_seconds, 'kind': 'measured rules only', 'model_calls': 0}
    for model in ('qwen35-4b', 'qwen35-9b'):
        predictions, summary = load_run(base / model, rows, definition, digest(inputs))
        methods[model] = predictions
        timings[model] = {'seconds': sum(p['seconds'] for p in predictions.values()),
                          'kind': 'measured warm per-paragraph calls, excluding loading and file writes',
                          'model_calls': len(rows), 'summary': summary}
    four = methods['qwen35-4b']
    methods['rules-and-4b'] = {r['id']: {'relevant': four[r['id']]['relevant'] if r['id'] in rule_ids else False} for r in rows}
    methods['rules-or-4b'] = {r['id']: {'relevant': True if r['id'] in rule_ids else four[r['id']]['relevant']} for r in rows}
    for name, ids in (('rules-and-4b', rule_ids), ('rules-or-4b', {r['id'] for r in rows} - rule_ids)):
        timings[name] = {'seconds': rule_seconds + sum(four[key]['seconds'] for key in ids),
                         'kind': 'offline replay estimate from measured calls; not separately timed pipeline',
                         'model_calls': len(ids)}
    scored = {method: {**metrics(rows, refs, predictions), 'timing': timings[method],
                       'by_paper': {pmid: metrics([r for r in rows if r['pmid'] == pmid], refs, predictions)
                                    for pmid in dict.fromkeys(r['pmid'] for r in rows)}}
              for method, predictions in methods.items()}
    cases = []
    for row in rows:
        cases.append({**row, 'reference': refs[row['id']],
                      'predictions': {name: predictions[row['id']] for name, predictions in methods.items()},
                      'rule_candidate': next((c for c in candidates['candidates'] if c['passage_id'] == row['id']), None)})
    result = {'input_sha256': digest(rows), 'reference_sha256': sha(reference_path),
              'reference_artifact': reference_path.name,
              'definition_sha256': sha(base / 'definition.json'), 'rule_source_sha256': sha(ROOT / 'apps/curation/pilot_candidates.py'),
              'documents': inputs['documents'], 'methods': scored, 'cases': cases,
              'limitations': ['Development sample of five papers, not independent production validation.',
                             'Astra is a model reference, not human gold.',
                             'Recall denominator is parsed paragraphs only; repeated text is not deduplicated knowledge.',
                             'Null/invalid model output is pending; on reference positives it counts as missed.',
                             'Literal full-paragraph retrieval validates provenance, not semantic truth.']}
    with output.open('x') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({name: {k: m[k] for k in ('tp', 'fp', 'fn', 'precision', 'recall', 'selected_all', 'pending_all')}
                      for name, m in scored.items()}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=ROOT / 'var/experiments/evidence-passages-20261023')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    evaluate(args.base, args.output or args.base / 'evaluation.json', args.reference)
