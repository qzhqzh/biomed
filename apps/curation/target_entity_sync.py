"""Automatic entity prerequisites for target exports, with an isolated local runner.

Operations carry local identities, never predicted target UUIDs. The target must
return an ID after an idempotent lookup/create. Only a SQLite preview adapter is
provided here; no production connection or credentials are accepted.
"""

import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path

OPERATION_SCHEMA = "biomed.target-entity-operation.v1"
PREVIEW_SCHEMA = "biomed.target-entity-preview.v1"
DRUG_CONTRACT = {
    "package": "drf_somatic_biomeddb==3.41.1",
    "model_sha256": "21ed906ab9b045643a95bb0a42895982f4eadb3be5e1c2d1cc2a067b5a79a80e",
    "serializer_sha256": "6e6d1b12c491cff331d025d6e9b9d8b3ab99cc0c673229f0e7f6d62aa212145e",
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def name_key(value):
    text = unicodedata.normalize("NFKC", value).translate(str.maketrans("‐‑‒–—", "-----"))
    return re.sub(r"\s+", " ", text.strip()).casefold()


def drug_operation(canonical):
    """Called only after a unique confirmed rule and its source support resolve."""
    local_id = str(uuid.UUID(str(canonical["id"])))
    provenance = canonical.get("provenance") or {}
    level = provenance.get("entity_level") or (provenance.get("local_standard") or {}).get("entity_level")
    if (canonical.get("status") != "active" or canonical.get("kind") != "drug"
            or level not in {"drug_ingredient", "molecule"}
            or (canonical.get("external_ids") or {}).get("tumor_api")):
        raise ValueError("Automatic drug creation requires an active, identified ingredient or molecule without a target ID")
    name, english = canonical.get("name"), canonical.get("name_en", "")
    if (not isinstance(name, str) or not name.strip() or len(name) > 255
            or not isinstance(english, str) or len(english) > 255):
        raise ValueError("Target drug names must fit the actual 255-character fields")
    # name_cn is the target's mandatory display name. Keep an untranslated
    # original name when no Chinese name exists; never invent a translation.
    payload = {"code": f"biomed:{local_id}", "name_cn": name.strip(), "name_en": english.strip(),
               "description": f"biomed canonical entity {local_id}; identity provenance retained in the companion package."}
    identity = {"id": local_id, "kind": "drug", "status": "active", "name": name,
                "name_en": english, "external_ids": copy.deepcopy(canonical.get("external_ids") or {}),
                "provenance": {"entity_level": level},
                "confirmed_global_aliases": sorted(set(canonical.get("confirmed_global_aliases", [])))}
    return {"schema_version": OPERATION_SCHEMA, "operation_id": f"ensure-drug:{local_id}",
            "action": "ensure_drug", "local_identity": identity, "create_payload": payload,
            "contract": copy.deepcopy(DRUG_CONTRACT), "requires_human_approval": False}


def operation_index(operations):
    """Validate operation fields and deduplicate repeated literature references."""
    if not isinstance(operations, list):
        raise TypeError("Entity operations must be an array")
    indexed = {}
    for operation in operations:
        try:
            expected = drug_operation(operation["local_identity"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid automatic drug operation identity") from error
        if operation != expected:
            raise ValueError("Invalid or modified automatic drug operation")
        key = operation["operation_id"]
        if key in indexed and indexed[key] != operation:
            raise ValueError("Conflicting definitions for one automatic entity operation")
        indexed[key] = operation
    return indexed


class PreviewTarget:
    """Persistent target-shaped test dictionary; refuses any existing foreign DB.

    The receipt and new drug are committed together. BEGIN IMMEDIATE serializes
    retries/concurrent preview workers; name_cn has the real target's UNIQUE
    constraint. A future production adapter must provide equivalent atomicity.
    """

    def __init__(self, path, dictionary):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.dictionary = copy.deepcopy(dictionary)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        db = self.connection
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if tables != {"preview_meta", "preview_drug", "preview_binding"}:
                    raise ValueError("Refusing to write an existing non-preview database")
                saved = dict(db.execute("SELECT key, value FROM preview_meta"))
                if saved != {"schema": PREVIEW_SCHEMA, "dictionary_sha256": fingerprint(dictionary)}:
                    raise ValueError("Preview database belongs to another dictionary snapshot")
            else:
                db.execute("CREATE TABLE preview_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE preview_drug (id TEXT PRIMARY KEY, name_cn TEXT UNIQUE NOT NULL, name_en TEXT NOT NULL, code TEXT NOT NULL, row_json TEXT NOT NULL)")
                db.execute("CREATE TABLE preview_binding (operation_id TEXT PRIMARY KEY, operation_sha256 TEXT NOT NULL, drug_id TEXT NOT NULL REFERENCES preview_drug(id))")
                db.executemany("INSERT INTO preview_meta VALUES (?, ?)",
                               [("schema", PREVIEW_SCHEMA), ("dictionary_sha256", fingerprint(dictionary))])
                for row in dictionary.get("drugs", []):
                    self._insert(row)
            db.commit()
        except Exception:
            db.rollback()
            db.close()
            raise

    def _insert(self, row):
        self.connection.execute("INSERT INTO preview_drug VALUES (?, ?, ?, ?, ?)",
                                (str(uuid.UUID(row["id"])), row.get("name_cn") or row["name"],
                                 row.get("name_en", ""), row.get("code", ""), json.dumps(row, ensure_ascii=False)))

    def close(self):
        self.connection.close()

    def ensure_drug(self, operation):
        operation_index([operation])
        db, key = self.connection, operation["operation_id"]
        checksum = fingerprint(operation)
        try:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute("SELECT * FROM preview_binding WHERE operation_id=?", (key,)).fetchone()
            if binding:
                row = db.execute("SELECT row_json FROM preview_drug WHERE id=?", (binding["drug_id"],)).fetchone()
                if row is None:
                    raise ValueError("Saved entity binding has no target record")
                result, action = json.loads(row[0]), "reused_binding"
            else:
                payload = operation["create_payload"]
                names = {name_key(payload[k]) for k in ("name_cn", "name_en") if payload[k]}
                names.update(name_key(alias) for alias in operation["local_identity"]["confirmed_global_aliases"])
                matches = []
                for stored in db.execute("SELECT row_json FROM preview_drug"):
                    row = json.loads(stored[0])
                    terms = [row.get(k, "") for k in ("name", "name_cn", "name_en")] + row.get("aliases", [])
                    if row.get("code") == payload["code"] or names & {name_key(term) for term in terms if term}:
                        matches.append(row)
                if len(matches) > 1:
                    raise ValueError("Multiple target drugs match this identity; no creation or arbitrary merge")
                if matches:
                    result, action = matches[0], "reused_name"
                else:
                    result = {**payload, "id": str(uuid.uuid4()), "name": payload["name_cn"], "aliases": [],
                              "external_ids": operation["local_identity"]["external_ids"], "preview_created": True}
                    self._insert(result)
                    action = "created"
                db.execute("INSERT INTO preview_binding VALUES (?, ?, ?)", (key, checksum, result["id"]))
            # Additional confirmed aliases do not require a new target identity.
            # Contradictory standard IDs, including on a saved binding, do.
            local_ids = operation["local_identity"]["external_ids"]
            target_ids = result.get("external_ids") or {}
            if any(namespace in target_ids and target_ids[namespace] != value
                   for namespace, value in local_ids.items()):
                raise ValueError("Target name or saved binding matches but standard identities conflict")
            db.execute("UPDATE preview_binding SET operation_sha256=? WHERE operation_id=?", (checksum, key))
            db.commit()
            return {"operation_id": key, "operation_sha256": checksum, "action": action,
                    "target_id": result["id"], "target_entity": result, "environment": "local_preview"}
        except Exception:
            db.rollback()
            raise

    def snapshot(self):
        result = copy.deepcopy(self.dictionary)
        result["source"] = {"snapshot_id": "preview:" + fingerprint(self.dictionary),
                            "environment": "local_preview", "parent": copy.deepcopy(self.dictionary["source"])}
        result["drugs"] = [json.loads(row[0]) for row in self.connection.execute("SELECT row_json FROM preview_drug ORDER BY id")]
        return result


def preview_entity_sync(mappings, target):
    """Run all prerequisites and bind returned IDs, isolating per-entity conflicts."""
    operations = operation_index([operation for mapping in mappings.values()
                                  for operation in mapping.get("entity_operations", [])])
    results, failures = {}, {}
    for key, operation in operations.items():
        try:
            results[key] = target.ensure_drug(operation)
        except (ValueError, sqlite3.Error) as error:
            failures[key] = str(error)
    bound = copy.deepcopy(mappings)
    for mapping in bound.values():
        for ref, key in mapping.pop("entity_operation_refs", {}).items():
            if key in results:
                mapping["entity_ids"][ref] = results[key]["target_id"]
                mapping.setdefault("entity_sync_receipts", {})[ref] = copy.deepcopy(results[key])
            else:
                mapping.setdefault("unresolved_entity_refs", []).append(ref)
                mapping.setdefault("identity_diagnostics", {})[ref] = {
                    "status": "target_entity_sync_failed", "detail": failures.get(key, "Missing automatic entity operation")}
        mapping.pop("entity_operations", None)
    receipt = {"schema_version": PREVIEW_SCHEMA, "environment": "local_preview", "operations": list(results.values()),
               "failures": failures, "created_count": sum(row["action"] == "created" for row in results.values()),
               "reused_count": sum(row["action"] != "created" for row in results.values()),
               "target_contacted": False, "target_written": False}
    return bound, receipt
