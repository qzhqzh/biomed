"""Read four target dictionary IDs and two bounded site checks, without patient data."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "var/experiments/target-identity-scope-20261112-v1/target-preview-astra-reviewed-v1.json"
DICTIONARY = ROOT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
OUTPUT = Path("/tmp/biomed-two-target-identities-v1.json")

# Runs in the already running target container. It does not start Django and
# returns only the requested standard entities and bounded evidence counts.
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
    options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c application_name=biomed_two_target_readonly') as conn:
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only'] == 'on'
        cur.execute('SHOW transaction_isolation')
        isolation = cur.fetchone()['transaction_isolation']
        def rows(query, params):
            cur.execute(query, params)
            return cur.fetchall()
        drugs = rows('SELECT id, code, name_cn, name_en FROM drf_somatic_biomeddb_drug WHERE id = ANY(%s::uuid[])', [request['drug_ids']])
        tumors = rows('SELECT id, code, name_cn, name_en, name_en_abbr, is_blood_tumor FROM drf_somatic_biomeddb_tumor WHERE id = ANY(%s::uuid[])', [request['tumor_ids']])
        results = []
        for case in request['cases']:
            alleles = rows('SELECT a.id, a.code, g.symbol AS gene_symbol, a.type, a.variant, a."index", a.is_locked '
                'FROM drf_somatic_biomeddb_tumor_allele a JOIN drf_somatic_biomeddb_gene g ON a.gene_id=g.id '
                'WHERE g.symbol=%s AND (a."index" ILIKE %s OR a.variant ILIKE %s) ORDER BY a.id LIMIT 31',
                [case['gene'], '%' + case['variant'] + '%', '%' + case['variant'] + '%'])
            counts = rows("SELECT COUNT(DISTINCT c.id) AS same_exact_core_count, "
                "COUNT(DISTINCT c.id) FILTER (WHERE e.evidence_source ILIKE %s OR e.evidence_source ILIKE %s) AS same_source_count "
                "FROM biomeddb_evidence_trigger t JOIN biomeddb_evidence_conclusion c ON c.id=t.conclusion_id "
                "JOIN biomeddb_clinical_evidence e ON e.id=c.evidence_id "
                "JOIN biomeddb_evidence_drug d ON d.conclusion_id=c.id "
                "JOIN biomeddb_evidence_disease_scope s ON s.conclusion_id=c.id "
                "WHERE t.event_type='mutation' AND t.trigger_type='direct' AND t.gene_symbol=%s "
                "AND t.target_key = ANY(%s) AND d.drug_id=%s::uuid AND s.tumor_id=%s::uuid "
                "AND e.evidence_category='treatment'",
                ['%' + case['pmid'] + '%', '%' + case['doi'] + '%', case['gene'],
                 [case['variant'], 'p.' + case['variant'], 'P.' + case['variant']], case['drug_id'], case['tumor_id']])[0]
            results.append({'pmid': case['pmid'], 'gene': case['gene'], 'variant': case['variant'],
                'matching_alleles': alleles[:30], 'alleles_truncated': len(alleles) > 30, **counts})
    conn.rollback()
print(json.dumps({'read_at_utc': datetime.now(timezone.utc).isoformat(), 'isolation': isolation,
    'transaction_read_only': True, 'patient_data_queried': False, 'target_written': False,
    'drugs': drugs, 'tumors': tumors, 'cases': results}, ensure_ascii=False, default=str))
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe():
    package = json.loads(PREVIEW.read_text(encoding="utf-8"))
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    if package["submit_ready"] or package["target_written"] or package["record_count"] != 2:
        raise ValueError("Expected the two inactive, unsubmitted source-reviewed candidates")
    cases = []
    for item in package["records"]:
        payload = item["target_payload"]
        if payload["is_active"] is not False or len(payload["conclusion_inputs"]) != 1:
            raise ValueError("Candidate scope differs")
        conclusion = payload["conclusion_inputs"][0]
        if len(conclusion["drug_ids"]) != 1 or len(conclusion["disease_scope_inputs"]) != 1 or len(conclusion["trigger_inputs"]) != 1:
            raise ValueError("Expected one drug, one primary disease and one direct mutation")
        trigger = conclusion["trigger_inputs"][0]
        if trigger["event_type"] != "mutation" or trigger["trigger_type"] != "direct":
            raise ValueError("Candidate is not a direct mutation")
        cases.append({"pmid": item["pmid"], "doi": item["biomed_companion"]["article"]["doi"],
                      "drug_id": conclusion["drug_ids"][0], "tumor_id": conclusion["disease_scope_inputs"][0]["tumor_id"],
                      "gene": trigger["gene_symbol"], "variant": trigger["target_key"]})
    if {(c["pmid"], c["gene"], c["variant"]) for c in cases} != {("42719457", "EGFR", "L861Q"), ("42756523", "KRAS", "G12C")}:
        raise ValueError("Candidate identities differ")
    request = {"cases": cases, "drug_ids": sorted({c["drug_id"] for c in cases}),
               "tumor_ids": sorted({c["tumor_id"] for c in cases})}
    process = subprocess.run(["docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1", "zhuqin-tumor-api",
                              "python", "-B", "-c", READER], input=json.dumps(request), text=True,
                             capture_output=True, timeout=45, check=False)
    if process.returncode:
        # Connection errors can include confidential environment values.
        raise RuntimeError("Bounded target read-only query failed; no result created")
    result = json.loads(process.stdout)
    for kind, key in (("drugs", "drug_ids"), ("tumors", "tumor_ids")):
        old = {str(row["id"]): row for row in dictionary[kind] if str(row["id"]) in request[key]}
        new = {str(row["id"]): row for row in result[kind]}
        for identity in request[key]:
            if identity not in old:
                raise ValueError("Original target UUID missing from frozen dictionary")
            if identity in new:
                new[identity]["same_names_as_snapshot"] = all(
                    (new[identity].get(field) or "") == (old[identity].get(field) or "") for field in ("name_cn", "name_en"))
        result[f"{kind}_all_present"] = set(new) == set(request[key])
    result.update({"version": "biomed.two-target-identity-readonly.v1", "preview_sha256": sha(PREVIEW),
                   "dictionary_sha256": sha(DICTIONARY), "reader_sha256": hashlib.sha256(READER.encode()).hexdigest(),
                   "note": "Targeted current IDs and exact literal core only. No full-library equivalence or patient matching."})
    return result


if __name__ == "__main__":
    if OUTPUT.exists():
        raise FileExistsError("Keep the first result; choose a new version instead")
    result = probe()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"drugs_all_present": result["drugs_all_present"], "tumors_all_present": result["tumors_all_present"],
                      "cases": [{key: c[key] for key in ("pmid", "same_exact_core_count", "same_source_count")}
                                for c in result["cases"]], "read_only": result["transaction_read_only"]}, ensure_ascii=False))
