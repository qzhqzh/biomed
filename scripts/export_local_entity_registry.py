"""Freeze biomed's small local identity tables; never connect to tumor_api."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_knowledge_intake import freeze


def snapshot(database):
    database = Path(database).resolve(strict=True)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        entities, rules = [], []
        for source in connection.execute("SELECT * FROM curation_canonicalentity ORDER BY id"):
            row = dict(source)
            entities.append({"id": str(row["id"]), "key": row["key"], "kind": row["kind"],
                             "name": row["name"], "name_en": row["name_en"], "level": row["level"],
                             "main_id": row["main_cancer_id"], "status": row["status"],
                             "external_ids": json.loads(row["external_ids"]),
                             "revision": row["revision"], "provenance": json.loads(row["provenance"])})
        for source in connection.execute("SELECT * FROM curation_entityresolutionrule ORDER BY id"):
            row = dict(source)
            rules.append({"id": str(row["id"]), "kind": row["kind"], "alias": row["alias"],
                          "scope": {"type": row["scope_type"], "key": row["scope_key"]},
                          "canonical_id": row["canonical_entity_id"], "status": row["status"],
                          "action": row["action"], "reason": row["reason"], "origin": row["origin"],
                          "revision": row["revision"], "provenance": json.loads(row["provenance"])})
        connection.rollback()
    return {"schema_version": "biomed.entity-registry.snapshot.v1",
            "source": {"database": str(database), "read_at": datetime.now(timezone.utc).isoformat(),
                       "read_only": True, "target_contacted": False},
            "entities": entities, "rules": rules}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        value = json.loads(args.output.read_text())
        if (value.get("schema_version") != "biomed.entity-registry.snapshot.v1"
                or value.get("source", {}).get("database") != str(args.database.resolve())):
            raise ValueError("Existing registry snapshot has a different identity")
        state = "reused"
    else:
        value = snapshot(args.database)
        freeze(args.output, value)
        state = "created"
    print(json.dumps({"status": state, "entities": len(value["entities"]),
                      "rules": len(value["rules"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
