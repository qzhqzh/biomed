"""Verify every cited passage and freeze the source-only model reference."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quotes(value):
    if isinstance(value, dict):
        if {'passage_id', 'locator', 'quote'} <= value.keys():
            yield value
        for child in value.values():
            yield from quotes(child)
    elif isinstance(value, list):
        for child in value:
            yield from quotes(child)


def freeze(root, review_path):
    root, review_path = Path(root), Path(review_path)
    source_path = root / 'source-only.json'
    source = json.loads(source_path.read_text(encoding='utf-8'))
    review = json.loads(review_path.read_text(encoding='utf-8'))
    articles = {str(row['pmid']): row for row in source['articles']}
    records = review.get('records')
    if (review.get('model') != 'gpt-6-astra' or review.get('source_only_sha256') != sha(source_path)
            or not isinstance(records, list) or len(records) != 5 or len(articles) != 5
            or {str(r.get('pmid')) for r in records} != set(articles)
            or set(map(str, review.get('pmids', []))) != set(articles)):
        raise ValueError('Reference input, model or article identity differs')
    count = 0
    for row in records:
        passages = {str(p['id']): p for p in articles[str(row['pmid'])]['passages']}
        found = list(quotes(row))
        if not found:
            raise ValueError('Each article needs cited source evidence')
        for quote in found:
            passage = passages.get(str(quote['passage_id']))
            if (not passage or quote['locator'] != passage['locator'] or not isinstance(quote['quote'], str)
                    or not quote['quote'] or quote['quote'] not in passage['text']):
                raise ValueError(f"Quoted text differs from source for PMID {row['pmid']}")
            count += 1
    result = {'version': 'fusion-independent-source-reference-integrity-v1', 'articles': len(records),
              'source_quotes_verified': count, 'source_file_sha256': sha(source_path),
              'review_sha256': sha(review_path), 'reference_kind': 'independent_model_not_clinical_gold',
              'created_at': datetime.now(timezone.utc).isoformat()}
    destination, validation = root / 'astra-source-reference.json', root / 'astra-source-reference-validation.json'
    if destination.exists() or validation.exists():
        raise FileExistsError('A frozen reference already exists')
    with destination.open('xb') as stream:
        stream.write(review_path.read_bytes())
    with validation.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.reference), ensure_ascii=False))
