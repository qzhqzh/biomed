"""Read only the one target switch that determines treatment report routing."""

import hashlib
import json
import subprocess
from pathlib import Path

OUTPUT = Path("/tmp/biomed-target-treatment-mode-v1.json")
READER = r'''
import json, os
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
cfg = {k: os.environ['DJANGO_DATABASE_' + v] for k, v in {
    'dbname': 'NAME', 'user': 'USER', 'password': 'PASSWORD', 'host': 'HOST', 'port': 'PORT'
}.items()}
with psycopg.connect(**cfg, connect_timeout=8, row_factory=dict_row,
    options='-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000 -c application_name=biomed_treatment_mode_readonly') as conn:
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        assert cur.fetchone()['transaction_read_only'] == 'on'
        cur.execute("SELECT value->>'mode' AS mode, revision FROM system_runtime_config WHERE key=%s LIMIT 1", ['solid_treatment'])
        row = cur.fetchone()
    conn.rollback()
print(json.dumps({'read_at_utc': datetime.now(timezone.utc).isoformat(),
    'stored_mode': row['mode'] if row else None, 'effective_mode': row['mode'] if row else 'legacy',
    'revision': row['revision'] if row else 0, 'transaction_read_only': True, 'target_written': False}))
'''


def main():
    if OUTPUT.exists():
        raise FileExistsError("Keep the first result; choose a new version")
    completed = subprocess.run(["docker", "exec", "-i", "-e", "PYTHONDONTWRITEBYTECODE=1", "zhuqin-tumor-api",
                                "python", "-B", "-c", READER], capture_output=True, text=True, timeout=20, check=False)
    if completed.returncode:
        raise RuntimeError("Read-only treatment mode query failed; no result created")
    result = json.loads(completed.stdout)
    result["recognized"] = result["effective_mode"] in {"legacy", "combined"}
    result["reader_sha256"] = hashlib.sha256(READER.encode()).hexdigest()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"effective_mode": result["effective_mode"], "recognized": result["recognized"],
                      "read_only": result["transaction_read_only"]}))


if __name__ == "__main__":
    main()
