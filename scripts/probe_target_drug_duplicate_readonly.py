"""Inspect two exact lorlatinib IDs and their bounded knowledge references."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DICTIONARY = ROOT / 'var/experiments/target-compatibility-20261101/target-dictionary.json'
REFERENCE = ROOT / 'var/experiments/fusion-independent-20261113-v1/astra-source-reference.json'
OUTPUT = Path('/tmp/biomed-039-lorlatinib-target-readonly.json')

READER = r'''
import json, os, sys
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
request = json.load(sys.stdin)
cfg = {k: os.environ['DJANGO_DATABASE_' + v] for k, v in {
    'dbname': 'NAME', 'user': 'USER', 'password': 'PASSWORD', 'host': 'HOST', 'port': 'PORT'
}.items()}
with psycopg.connect(**cfg, connect_timeout=8, row_factory=dict_row,
    options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c application_name=biomed_drug_duplicate_readonly') as conn:
    conn.isolation_level=psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only=True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only']=='on'
        cur.execute('SELECT id,name_cn,name_en,code FROM drf_somatic_biomeddb_drug WHERE id=ANY(%s::uuid[]) ORDER BY id', [request['ids']])
        drugs=cur.fetchall()
        cur.execute('SELECT drug_id,COUNT(*) AS total FROM biomeddb_evidence_drug WHERE drug_id=ANY(%s::uuid[]) GROUP BY drug_id', [request['ids']])
        evidence=cur.fetchall()
        cur.execute('SELECT drug_id,COUNT(*) AS total FROM drf_somatic_biomeddb_tumor_drug WHERE drug_id=ANY(%s::uuid[]) GROUP BY drug_id', [request['ids']])
        legacy=cur.fetchall()
    conn.rollback()
print(json.dumps({'read_at_utc':datetime.now(timezone.utc).isoformat(),'transaction_read_only':True,
    'target_written':False,'patient_data_queried':False,'drugs':drugs,'evidence_center_counts':evidence,
    'legacy_tumor_drug_counts':legacy},ensure_ascii=False,default=str))
'''


def main():
    if OUTPUT.exists():
        raise FileExistsError('Preserve first read-only target result')
    dictionary = json.loads(DICTIONARY.read_text(encoding='utf-8'))
    reference = json.loads(REFERENCE.read_text(encoding='utf-8'))
    if not any(str(row.get('pmid')) == '41059188' for row in reference.get('records', [])):
        raise ValueError('Frozen fusion article reference is missing')
    matches = [row for row in dictionary['drugs'] if str(row.get('name_en') or '').casefold() == 'lorlatinib']
    if len(matches) != 2:
        raise ValueError('Expected two distinct target dictionary IDs')
    ids = sorted(str(row['id']) for row in matches)
    process = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'zhuqin-tumor-api',
                              'python', '-B', '-c', READER], input=json.dumps({'ids': ids}), text=True,
                             capture_output=True, timeout=45, check=False)
    if process.returncode:
        error = re.search(r'psycopg\.errors\.([A-Za-z0-9]+)', process.stderr)
        raise RuntimeError(f'Read-only duplicate lookup failed ({error.group(1) if error else "unclassified"}); no result created')
    result = json.loads(process.stdout)
    result['same_ids_as_snapshot'] = sorted(str(row['id']) for row in result['drugs']) == ids
    result.update({'version': 'target-lorlatinib-duplicate-readonly-v1',
                   'dictionary_sha256': hashlib.sha256(DICTIONARY.read_bytes()).hexdigest(),
                   'source_reference_sha256': hashlib.sha256(REFERENCE.read_bytes()).hexdigest(),
                   'reader_sha256': hashlib.sha256(READER.encode()).hexdigest(),
                   'scope': 'Two drug dictionary IDs and knowledge-link counts only; no patient data or writes.'})
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'same_ids_as_snapshot': result['same_ids_as_snapshot'], 'drugs': result['drugs'],
                      'evidence_center_counts': result['evidence_center_counts'],
                      'legacy_tumor_drug_counts': result['legacy_tumor_drug_counts'], 'read_only': result['transaction_read_only']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
