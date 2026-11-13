"""Verify Astra's exact quotations before preserving the fusion review."""

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
    source_path = root / 'source-only.json'
    source = json.loads(source_path.read_text(encoding='utf-8'))
    for name in ('fusion-development-v1.json', 'source-only.json', 'astra-baseline-review.json'):
        if review.get('input_sha256', {}).get(name) != sha(root / name):
            raise ValueError(f'Review input mismatch: {name}')
    if (review.get('model') != 'gpt-6-astra' or review.get('pmid') != '42820355'
            or review.get('group_id') != 'E-42820355-0021'
            or review.get('judgment') not in {'supported', 'partial', 'unsupported', 'uncertain'}):
        raise ValueError('Unexpected review identity')
    article = next(row for row in source['articles'] if str(row['pmid']) == review['pmid'])
    passages = {str(p['id']): p for p in article['passages']}
    evidence = review.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('Review lacks quotations')
    for quote in evidence:
        passage = passages.get(str(quote.get('passage_id')))
        text = quote.get('quote')
        if (not passage or quote.get('locator') != passage.get('locator') or not isinstance(text, str)
                or not text or text not in passage.get('text', '')):
            raise ValueError('Review quotation does not match the cited source passage')
    result = {'version': 'target-fusion-astra-review-integrity-v1', 'pmid': review['pmid'],
              'judgment': review['judgment'], 'source_fact_judgment': review['source_fact_judgment'],
              'source_quotes_verified': len(evidence), 'review_sha256': sha(review_path),
              'reference_kind': 'independent_astra_model_assessment_not_expert_gold',
              'created_at': datetime.now(timezone.utc).isoformat()}
    destination, validation = root / 'astra-fusion-review.json', root / 'astra-fusion-review-validation.json'
    if destination.exists() or validation.exists():
        raise FileExistsError('Frozen fusion review already exists')
    with destination.open('xb') as stream:
        stream.write(review_path.read_bytes())
    with validation.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--review', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.review), ensure_ascii=False))
