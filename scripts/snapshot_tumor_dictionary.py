"""Read only target knowledge dictionaries; never start Django or touch patients.

Credentials stay inside the existing target container. All queries are explicit
SELECTs in one PostgreSQL REPEATABLE READ / READ ONLY transaction. Only the
snapshot is written, in biomed's artifact directory.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

READER = r'''
import json, os
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row

cfg = {k: os.environ['DJANGO_DATABASE_' + v] for k, v in {
    'dbname': 'NAME', 'user': 'USER', 'password': 'PASSWORD', 'host': 'HOST', 'port': 'PORT'
}.items()}
genes = ['H3F3A', 'H3-3A', 'HIST1H3B', 'HIST1H3C', 'H3C2', 'H3C3', 'KRAS', 'PHF6', 'APC']
with psycopg.connect(**cfg, connect_timeout=8, row_factory=dict_row,
    options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c application_name=biomed_dictionary_readonly') as conn:
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only'] == 'on'
        cur.execute('SHOW transaction_isolation')
        isolation = cur.fetchone()['transaction_isolation']
        def rows(query, params=None):
            cur.execute(query, params)
            return cur.fetchall()
        drugs = rows('SELECT id, code, name_cn, name_en FROM drf_somatic_biomeddb_drug ORDER BY id')
        tumors = rows('SELECT id, code, name_cn, name_en, name_en_abbr, is_blood_tumor FROM drf_somatic_biomeddb_tumor ORDER BY id')
        parents = rows('SELECT tumor_id, parent_tumor_id, version FROM drf_somatic_biomeddb_tumor_tree ORDER BY tumor_id,parent_tumor_id,version')
        rules = rows('SELECT id, code, name, description, expression, expression_hash, version, is_active, review_status, is_locked FROM biomeddb_target_rule ORDER BY id')
        conditions = rows('SELECT target_rule_id, code, event_type, gene_symbol, target_key, distinct_event, sort_order FROM biomeddb_target_rule_condition ORDER BY target_rule_id,sort_order,code')
        targets = rows("SELECT DISTINCT event_type,gene_symbol,target_key FROM biomeddb_evidence_trigger WHERE trigger_type='direct' AND gene_symbol=ANY(%s) ORDER BY event_type,gene_symbol,target_key", [genes])
        alleles = rows('SELECT a.id, a.code, g.symbol AS gene_symbol, a.type, a.variant, a."index", a.is_locked FROM drf_somatic_biomeddb_tumor_allele a JOIN drf_somatic_biomeddb_gene g ON a.gene_id=g.id WHERE g.symbol=ANY(%s) ORDER BY g.symbol,a.id', [genes])
    conn.rollback()
timestamp = datetime.now(timezone.utc).isoformat()
for row in drugs:
    row['name'] = row['name_en'] or row['name_cn']
    row['aliases'] = sorted({s for s in [row['name_en'], row['name_cn']] if s and s != row['name']})
for row in tumors:
    row['name'] = row['name_en'] or row['name_cn']
    row['aliases'] = sorted({s for s in [row['name_en'], row['name_cn'], row['name_en_abbr']] if s and s != row['name']})
    row['parents'] = [p for p in parents if p['tumor_id'] == row['id']]
for row in rules:
    row['conditions'] = [c for c in conditions if c['target_rule_id'] == row['id']]
for row in targets:
    row['source_table'] = 'biomeddb_evidence_trigger'
result = {
    'schema_version': 'tumor-api-dictionary-v1',
    'source': {'snapshot_id': 'tumor-api-knowledge-' + timestamp,
        'read_at_utc': timestamp, 'read_only': True, 'isolation': isolation,
        'reader': 'standalone_psycopg_no_django', 'target_written': False,
        'scope': 'Drug, Tumor, TumorTree, TargetRule/conditions; relevant knowledge triggers/alleles only',
        'logical_target_gene_scope': genes, 'logical_targets_complete_for_all_genes': False},
    'drugs': drugs, 'tumors': tumors, 'rules': rules,
    'logical_targets': targets, 'relevant_alleles': alleles,
}
print(json.dumps(result, ensure_ascii=False, default=str))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='zhuqin-tumor-api')
    parser.add_argument('--target-source', type=Path, default=Path('/home/zhuqin/project/zhenyuan/tumor_api'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Snapshot already exists; reuse it or choose a new version')
    run = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                          args.container, 'python', '-B', '-'], input=READER,
                         capture_output=True, text=True, timeout=90, check=False)
    if run.returncode:
        # Driver tracebacks can contain connection details; keep them out of output.
        raise RuntimeError('Read-only dictionary query failed; no snapshot created')
    data = json.loads(run.stdout)
    source = data['source']
    source['container'] = args.container
    source['reader_sha256'] = hashlib.sha256(READER.encode()).hexdigest()
    source['target_commit'] = subprocess.check_output(
        ['git', '-C', str(args.target_source), 'rev-parse', 'HEAD'], text=True).strip()
    files = ['src/biomeddb/models/evidence_center.py', 'src/biomeddb/serializers/evidence_center.py',
             'src/biomeddb/services/evidence_center.py', 'src/biomeddb/services/logical_target.py']
    source['source_files'] = {name: hashlib.sha256((args.target_source / name).read_bytes()).hexdigest()
                              for name in files}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as out:
        out.write(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'counts': {k: len(data[k]) for k in
          ['drugs', 'tumors', 'rules', 'logical_targets', 'relevant_alleles']},
          'read_only': True, 'target_written': False}))


if __name__ == '__main__':
    main()
