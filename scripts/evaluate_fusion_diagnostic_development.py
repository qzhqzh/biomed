"""Research-only replay of precise fusion diagnoses on inspected challenge cases."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_fusion_diagnostic_preview import preview
from scripts.freeze_effective_knowledge_sample import materialize, queue_rows
from scripts.knowledge_observation_report import DEFAULT_ROOT

PROJECT = Path(__file__).resolve().parents[1]
DICTIONARY = PROJECT / 'var/experiments/target-compatibility-20261101/target-dictionary.json'
GENES = PROJECT / 'data/knowledge/genes.jsonl'
PMIDS = {'42834928', '42828699'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(root, output, names, reference):
    root, output, names, reference = map(Path, (root, output, names, reference))
    if output.exists():
        raise FileExistsError('Preserve the first development result')
    selected = [row for row in queue_rows(root) if row['pmid'] in PMIDS]
    if {r['pmid'] for r in selected} != PMIDS:
        raise ValueError('Challenge source selection differs')
    dictionary = json.loads(DICTIONARY.read_text(encoding='utf-8'))
    refs = json.loads(names.read_text(encoding='utf-8'))
    independent = json.loads(reference.read_text(encoding='utf-8'))
    if (not PMIDS <= {str(row['pmid']) for row in independent.get('records', [])}
            or independent.get('source_only_sha256') != sha(reference.parent / 'source-only.json')):
        raise ValueError('Independent source reference or its frozen input differs')
    standard = load_gene_alias_standard(GENES)
    records = []
    for sample in materialize(root, selected):
        if sample['source_manifest'].get('dictionary_sha256') != sha(DICTIONARY):
            raise ValueError('Frozen target dictionary differs')
        old = bool(sample['original_export'].get('offline_ready'))
        result = {'status': 'baseline_preserved', 'reason': 'original_offline_ready_preserved'} if old else preview(
            sample, dictionary=dictionary, gene_standard=standard, name_references=refs)
        records.append({'pmid': sample['pmid'], 'group_id': sample['group_id'],
                        'sample_sha256': sample['sample_sha256'], 'baseline_offline_ready': old, 'preview': result})
    per_article = {}
    for pmid in sorted(PMIDS):
        case = [r for r in records if r['pmid'] == pmid]
        per_article[pmid] = {'candidates': len(case), 'old_offline_ready': sum(r['baseline_offline_ready'] for r in case),
                             'inactive_diagnostic_previews': sum(r['preview']['status'] == 'inactive_structure_preview' for r in case),
                             'reasons': dict(Counter(r['preview']['reason'] for r in case))}
    result = {'version': 'fusion-diagnostic-development-v1', 'articles': 2, 'candidates': len(records),
              'per_article': per_article, 'records': records, 'developed_after_reading_reference': True,
              'code_sha256': sha(PROJECT/'apps/curation/source_fusion_diagnostic_preview.py'),
              'name_reference_sha256': sha(names), 'source_reference_sha256': sha(reference),
              'target_written': False, 'production_written': False,
              'note': 'Exploratory development on inspected cases; no independent estimate of diagnostic yield.'}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {'articles': 2, 'candidates': len(records), 'per_article': per_article}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--names', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output, args.names, args.reference), ensure_ascii=False))
