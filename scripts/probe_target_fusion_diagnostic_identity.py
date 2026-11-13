"""Read one target cancer ID and literal diagnostic fusion key without writes."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / 'var/experiments/fusion-independent-20261113-v1/diagnostic-development.json'
OUTPUT = Path('/tmp/biomed-039-diagnostic-identity-readonly-v1.json')

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
    options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c application_name=biomed_diagnostic_fusion_readonly') as conn:
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only'] == 'on'
        cur.execute('SELECT id,code,name_cn,name_en,is_blood_tumor FROM drf_somatic_biomeddb_tumor WHERE id=%s::uuid', [request['tumor_id']])
        tumors = cur.fetchall()
        cur.execute("SELECT COUNT(DISTINCT c.id) AS same_exact_core_count, "
            "COUNT(DISTINCT c.id) FILTER (WHERE e.evidence_source ILIKE %s) AS same_source_count "
            "FROM biomeddb_evidence_trigger t JOIN biomeddb_evidence_conclusion c ON c.id=t.conclusion_id "
            "JOIN biomeddb_clinical_evidence e ON e.id=c.evidence_id "
            "JOIN biomeddb_evidence_disease_scope s ON s.conclusion_id=c.id "
            "WHERE t.event_type='fusion' AND t.trigger_type='direct' AND upper(t.gene_symbol) IN ('COL1A1','PDGFB') "
            "AND regexp_replace(upper(t.target_key), '[^A-Z0-9]', '', 'g')='COL1A1PDGFB' "
            "AND s.tumor_id=%s::uuid AND e.evidence_category='diagnostic'", ['%42834928%',request['tumor_id']])
        counts = cur.fetchone()
    conn.rollback()
print(json.dumps({'read_at_utc':datetime.now(timezone.utc).isoformat(),'transaction_read_only':True,
    'target_written':False,'patient_data_queried':False,'tumors':tumors,'exact_key_counts':counts},ensure_ascii=False,default=str))
'''


def main():
    if OUTPUT.exists():
        raise FileExistsError('Preserve first identity result')
    data = json.loads(DEVELOPMENT.read_text(encoding='utf-8'))
    rows = [r for r in data['records'] if r.get('preview', {}).get('status') == 'inactive_structure_preview']
    if len(rows) != 1 or rows[0]['group_id'] != 'E-42834928-0014':
        raise ValueError('Unexpected diagnostic preview')
    preview = rows[0]['preview']
    payload = preview['target_payload']
    conclusion = payload['conclusion_inputs'][0]
    if (preview['submit_ready'] or payload['is_active'] or payload['evidence_category'] != 'diagnostic'
            or conclusion['trigger_inputs'][0]['target_key'] != 'COL1A1::PDGFB'):
        raise ValueError('Unexpected target identity or activation state')
    request = {'tumor_id': conclusion['disease_scope_inputs'][0]['tumor_id']}
    process = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'zhuqin-tumor-api',
                              'python', '-B', '-c', READER], input=json.dumps(request), text=True,
                             capture_output=True, timeout=45, check=False)
    if process.returncode:
        raise RuntimeError('Bounded target read-only lookup failed; no result created')
    result = json.loads(process.stdout)
    result['tumor_id_present_and_name_matches'] = (len(result['tumors']) == 1
        and str(result['tumors'][0]['id']) == request['tumor_id']
        and result['tumors'][0]['name_cn'] == '隆突性皮肤纤维肉瘤')
    result.update({'version': 'biomed.target-fusion-diagnostic-identity-readonly.v1',
                   'development_sha256': hashlib.sha256(DEVELOPMENT.read_bytes()).hexdigest(),
                   'reader_sha256': hashlib.sha256(READER.encode()).hexdigest(),
                   'scope': 'One target ID and exact diagnostic key only; no patient data or semantic full-library search.'})
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'tumor_id_present_and_name_matches': result['tumor_id_present_and_name_matches'],
                      'exact_key_counts': result['exact_key_counts'], 'read_only': result['transaction_read_only']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
