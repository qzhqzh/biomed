"""Check two target identifiers and a bounded exact fusion evidence key read-only."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / 'var/experiments/target-holdout-20261113-v1/fusion-development-v1.json'
DICTIONARY = ROOT / 'var/experiments/target-compatibility-20261101/target-dictionary.json'
OUTPUT = Path('/tmp/biomed-038-fusion-identities-readonly-v1.json')

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
    options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c application_name=biomed_fusion_readonly') as conn:
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only'] == 'on'
        cur.execute('SELECT id, code, name_cn, name_en FROM drf_somatic_biomeddb_drug WHERE id = %s::uuid', [request['drug_id']])
        drugs = cur.fetchall()
        cur.execute('SELECT id, code, name_cn, name_en, is_blood_tumor FROM drf_somatic_biomeddb_tumor WHERE id = %s::uuid', [request['tumor_id']])
        tumors = cur.fetchall()
        cur.execute("SELECT COUNT(DISTINCT c.id) AS same_exact_core_count, "
            "COUNT(DISTINCT c.id) FILTER (WHERE e.evidence_source ILIKE %s OR e.evidence_source ILIKE %s) AS same_source_count "
            "FROM biomeddb_evidence_trigger t JOIN biomeddb_evidence_conclusion c ON c.id=t.conclusion_id "
            "JOIN biomeddb_clinical_evidence e ON e.id=c.evidence_id "
            "JOIN biomeddb_evidence_drug d ON d.conclusion_id=c.id "
            "JOIN biomeddb_evidence_disease_scope s ON s.conclusion_id=c.id "
            "WHERE t.event_type='fusion' AND t.trigger_type='direct' AND upper(t.gene_symbol) IN ('EML4','ALK') "
            "AND regexp_replace(upper(t.target_key), '[^A-Z0-9]', '', 'g')='EML4ALK' "
            "AND d.drug_id=%s::uuid AND s.tumor_id=%s::uuid AND e.evidence_category='treatment'",
            ['%42820355%', '%10.2340/1651-226X.2026.46424%', request['drug_id'], request['tumor_id']])
        counts = cur.fetchone()
    conn.rollback()
print(json.dumps({'read_at_utc': datetime.now(timezone.utc).isoformat(), 'transaction_read_only': True,
    'patient_data_queried': False, 'target_written': False, 'drugs': drugs, 'tumors': tumors,
    'exact_key_counts': counts}, ensure_ascii=False, default=str))
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    if OUTPUT.exists():
        raise FileExistsError('Preserve the first read-only result')
    development, dictionary = (json.loads(path.read_text(encoding='utf-8')) for path in (DEVELOPMENT, DICTIONARY))
    case = next(r for r in development['records'] if r['group_id'] == 'E-42820355-0021')
    preview = case['preview']
    payload = preview['target_payload']
    conclusion = payload['conclusion_inputs'][0]
    trigger = conclusion['trigger_inputs'][0]
    if (preview['submit_ready'] or payload['is_active'] or trigger != {'event_type': 'fusion',
            'gene_symbol': 'ALK', 'target_key': 'EML4::ALK', 'trigger_type': 'direct'}):
        raise ValueError('Unexpected preview identity or activation state')
    request = {'drug_id': conclusion['drug_ids'][0], 'tumor_id': conclusion['disease_scope_inputs'][0]['tumor_id']}
    process = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'zhuqin-tumor-api',
                              'python', '-B', '-c', READER], input=json.dumps(request), text=True,
                             capture_output=True, timeout=45, check=False)
    if process.returncode:
        raise RuntimeError('Bounded target read-only query failed; no result created')
    result = json.loads(process.stdout)
    for kind, key in [('drugs', 'drug_id'), ('tumors', 'tumor_id')]:
        historical = next((r for r in dictionary[kind] if str(r['id']) == request[key]), None)
        current = result[kind]
        result[kind + '_present'] = len(current) == 1 and str(current[0]['id']) == request[key]
        result[kind + '_names_match_snapshot'] = bool(historical and result[kind + '_present'] and all(
            (current[0].get(field) or '') == (historical.get(field) or '') for field in ('name_cn', 'name_en')))
    result.update({'version': 'biomed.target-fusion-identity-readonly.v1', 'development_sha256': sha(DEVELOPMENT),
                   'dictionary_sha256': sha(DICTIONARY), 'reader_sha256': hashlib.sha256(READER.encode()).hexdigest(),
                   'scope': 'Two dictionary IDs and literal ordered fusion core only; no patient or semantic full-library search.'})
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'drugs_present': result['drugs_present'], 'tumors_present': result['tumors_present'],
                      'snapshot_names_match': result['drugs_names_match_snapshot'] and result['tumors_names_match_snapshot'],
                      'exact_key_counts': result['exact_key_counts'], 'read_only': result['transaction_read_only']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
