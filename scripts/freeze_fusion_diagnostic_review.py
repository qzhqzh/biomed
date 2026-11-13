"""Verify the Astra diagnostic review and preserve its source quotations."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(root, review_path):
    root, review_path = Path(root), Path(review_path)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    for name in ('diagnostic-development.json', 'source-only.json', 'diagnostic-name-reference.json'):
        if review.get('input_sha256', {}).get(name) != sha(root / name):
            raise ValueError(f'Review input differs: {name}')
    if (review.get('model') != 'gpt-6-astra' or review.get('pmid') != '42834928'
            or review.get('group_id') != 'E-42834928-0014'
            or review.get('core_source_fact_judgment') != 'supported'
            or review.get('complete_preview_judgment') != 'partial'):
        raise ValueError('Unexpected reference identity or conclusion')
    source = json.loads((root / 'source-only.json').read_text(encoding='utf-8'))
    article = next(a for a in source['articles'] if a['pmid'] == review['pmid'])
    passages = {p['id']: p for p in article['passages']}
    evidence = review.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('Review has no cited evidence')
    for item in evidence:
        passage = passages.get(item.get('passage_id'))
        if (not passage or item.get('locator') != passage['locator'] or not isinstance(item.get('quote'), str)
                or not item['quote'] or item['quote'] not in passage['text']):
            raise ValueError('Astra quotation differs from cited source')
    validation = {'version': 'fusion-diagnostic-astra-review-integrity-v1', 'pmid': review['pmid'],
                  'core_judgment': review['core_source_fact_judgment'], 'preview_judgment': review['complete_preview_judgment'],
                  'source_quotes_verified': len(evidence), 'review_sha256': sha(review_path),
                  'reference_kind': 'independent_model_not_clinical_gold', 'created_at': datetime.now(timezone.utc).isoformat()}
    destination, result = root / 'astra-diagnostic-review.json', root / 'astra-diagnostic-review-validation.json'
    if destination.exists() or result.exists():
        raise FileExistsError('Frozen diagnostic review exists')
    with destination.open('xb') as stream:
        stream.write(review_path.read_bytes())
    with result.open('x', encoding='utf-8') as stream:
        json.dump(validation, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return validation


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--review', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.review), ensure_ascii=False))
