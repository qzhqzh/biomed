"""Append a source-verified correction without changing the blinded reference."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def freeze(root):
    root = Path(root)
    source_path, business_path = root / 'source-only.json', root / 'astra-business-reference.json'
    source = json.loads(source_path.read_text(encoding='utf-8'))
    business = json.loads(business_path.read_text(encoding='utf-8'))
    article = next(a for a in source['articles'] if str(a['pmid']) == '42848093')
    initial = next(a for a in business['records'] if str(a['pmid']) == '42848093')
    if initial['business_scope'] != 'outside_primary_scope':
        raise ValueError('Initial frozen reference is not the expected assessment')
    expected = [
        ('5bbd53cdf5754fe3a22ac2a272901def', 'body/p:39',
         'Two example cases from our institution are provided below (Figs. 2 and 3).'),
        ('d8f982b861f446ee89c18a16af7dbd2a', 'body/p:44',
         'A 12-year-old female presented with a one-week history of headaches and nausea.'),
        ('d8f982b861f446ee89c18a16af7dbd2a', 'body/p:44',
         'Molecular testing demonstrated a ZFTA-RELA fusion; homozygous CDKN2A/B deletion was also detected.'),
        ('d8f982b861f446ee89c18a16af7dbd2a', 'body/p:44',
         'After discharge, she completed adjuvant focal radiation (59.4 Gy in 33 fractions).'),
    ]
    passages = {p['id']: p for p in article['passages']}
    quotes = []
    for passage_id, locator, quote in expected:
        passage = passages[passage_id]
        if passage['locator'] != locator or quote not in passage['text']:
            raise ValueError('Correction quotation does not match source')
        quotes.append({'passage_id': passage_id, 'locator': locator, 'quote': quote})
    result = {'version': 'target-holdout-business-reference-correction-v1', 'pmid': '42848093',
              'model': 'gpt-6-astra', 'initial_business_scope': initial['business_scope'],
              'correction': 'This review includes an institutional human case with molecular events and actual treatment.',
              'unknowns': ['Whether first published here is unknown.',
                           'The case does not establish that either event predicts radiotherapy benefit.'],
              'quotes': quotes, 'source_quotes_verified': len(quotes),
              'source_file_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
              'initial_reference_sha256': hashlib.sha256(business_path.read_bytes()).hexdigest(),
              'reference_kind': 'followup_astra_source_reference_not_expert_gold',
              'created_at': datetime.now(timezone.utc).isoformat()}
    output = root / 'astra-business-reference-correction.json'
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return {'pmid': result['pmid'], 'source_quotes_verified': len(quotes)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root), ensure_ascii=False))
