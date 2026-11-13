"""Replay frozen fusion preview on every old candidate from two challenge articles."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_fusion_pair_preview import preview
from scripts.freeze_effective_knowledge_sample import materialize, queue_rows
from scripts.knowledge_observation_report import DEFAULT_ROOT

PROJECT = Path(__file__).resolve().parents[1]
DICTIONARY = PROJECT / 'var/experiments/target-compatibility-20261101/target-dictionary.json'
GENES = PROJECT / 'data/knowledge/genes.jsonl'
CONTROL_PMIDS = {'42834928', '42828699'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(root, output, source):
    root, output, source = Path(root), Path(output), Path(source)
    if output.exists():
        raise FileExistsError('Preserve the first control comparison')
    reference = json.loads(source.read_text(encoding='utf-8'))
    if not CONTROL_PMIDS <= {r['pmid'] for r in reference['records']}:
        raise ValueError('Reference is missing a control article')
    selected = [row for row in queue_rows(root) if row['pmid'] in CONTROL_PMIDS]
    if {r['pmid'] for r in selected} != CONTROL_PMIDS:
        raise ValueError('Expected two previously processed control articles')
    samples = materialize(root, selected)
    dictionary = json.loads(DICTIONARY.read_text(encoding='utf-8'))
    standard = load_gene_alias_standard(GENES)
    rows = []
    for sample in samples:
        if sample['source_manifest'].get('dictionary_sha256') != sha(DICTIONARY):
            raise ValueError('Frozen dictionary differs from the old candidate source')
        old = bool(sample['original_export'].get('offline_ready'))
        result = {'status': 'baseline_preserved', 'reason': 'original_offline_ready_preserved'} if old else preview(
            sample, dictionary=dictionary, gene_standard=standard)
        rows.append({'pmid': sample['pmid'], 'group_id': sample['group_id'],
                     'sample_sha256': sample['sample_sha256'], 'baseline_offline_ready': old, 'preview': result})
    per_article = {}
    for pmid in sorted(CONTROL_PMIDS):
        case = [r for r in rows if r['pmid'] == pmid]
        per_article[pmid] = {'old_candidates': len(case), 'old_offline_ready': sum(r['baseline_offline_ready'] for r in case),
                             'new_inactive_previews': sum(r['preview']['status'] == 'inactive_structure_preview' for r in case),
                             'reasons': dict(Counter(r['preview']['reason'] for r in case))}
    result = {'version': 'fusion-independent-existing-controls-v1', 'articles': 2, 'candidates': len(rows),
              'per_article': per_article, 'records': rows, 'code_sha256': sha(PROJECT/'apps/curation/source_fusion_pair_preview.py'),
              'dictionary_sha256': sha(DICTIONARY), 'reference_sha256': sha(source),
              'scope': 'Every previously saved candidate, not article-level recall; source-only reference was frozen separately.',
              'production_written': False, 'target_written': False}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {'articles': 2, 'candidates': len(rows), 'per_article': per_article}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output, args.reference), ensure_ascii=False))
