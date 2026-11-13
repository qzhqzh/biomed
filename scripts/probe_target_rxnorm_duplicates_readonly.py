"""Inspect bounded target drug-ID usage for exact RxNorm-name collisions.

This is an initial read-only reference snapshot.  It never selects an export
ID, changes a registry, reads patient data or contacts the target at runtime.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from apps.curation.knowledge_target_mapping import _target_name_key
from scripts.probe_target_drug_duplicate_readonly import READER


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(collisions_path, dictionary_path, output):
    collisions_path, dictionary_path, output = map(Path, (collisions_path, dictionary_path, output))
    if output.exists():
        raise FileExistsError('Preserve the first read-only target usage snapshot')
    report = json.loads(collisions_path.read_text(encoding='utf-8'))
    if report.get('dictionary_sha256') != sha(dictionary_path):
        raise ValueError('Collision report and dictionary snapshot differ')
    groups = [g for g in report['rxnorm_coverage']['groups'] if len(g['exact_rxnorm_ids']) == 1]
    ids = sorted({identifier for group in groups for identifier in group['target_ids']})
    if len(groups) > 50 or len(ids) > 100:
        raise ValueError('Read-only lookup exceeds its bounded research scope')
    process = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'zhuqin-tumor-api',
                              'python', '-B', '-c', READER], input=json.dumps({'ids': ids}), text=True,
                             capture_output=True, timeout=45, check=False)
    if process.returncode:
        raise RuntimeError('Read-only target lookup failed; no snapshot created')
    target = json.loads(process.stdout)
    if not target['transaction_read_only'] or target['target_written'] or target['patient_data_queried']:
        raise ValueError('Target reader did not confirm its read-only scope')
    drugs = {str(row['id']): row for row in target['drugs']}
    center = {str(row['drug_id']): row['total'] for row in target['evidence_center_counts']}
    legacy = {str(row['drug_id']): row['total'] for row in target['legacy_tumor_drug_counts']}
    observations = []
    for group in groups:
        present = all(identifier in drugs for identifier in group['target_ids'])
        names_unchanged = present and all(_target_name_key(drugs[identifier]['name_en']) == group['name_en_normalized']
                                          for identifier in group['target_ids'])
        used = [identifier for identifier in group['target_ids'] if center.get(identifier, 0) > 0]
        observations.append({**group, 'all_target_ids_present': present, 'english_names_unchanged': names_unchanged,
                             'center_counts': {identifier: center.get(identifier, 0) for identifier in group['target_ids']},
                             'legacy_counts': {identifier: legacy.get(identifier, 0) for identifier in group['target_ids']},
                             'center_used_id_count': len(used)})
    result = {'version': 'target-rxnorm-duplicate-usage-v1', 'read_at_utc': target['read_at_utc'],
              'dictionary_sha256': sha(dictionary_path), 'collision_report_sha256': sha(collisions_path),
              'reader_sha256': hashlib.sha256(READER.encode()).hexdigest(), 'observations': observations,
              'target_records_requested': len(ids), 'target_records_found': len(drugs),
              'groups_with_one_center_id_used': sum(g['all_target_ids_present'] and g['english_names_unchanged']
                                                   and g['center_used_id_count'] == 1 for g in observations),
              'scope': 'Knowledge reference counts only; no patient records, production writes or identity decisions.',
              'transaction_read_only': True, 'target_written': False, 'identity_decisions_made': False}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {key: result[key] for key in ('target_records_requested', 'target_records_found',
                                         'groups_with_one_center_id_used', 'target_written')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collisions', type=Path, required=True)
    parser.add_argument('--dictionary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.collisions, args.dictionary, args.output), ensure_ascii=False))
