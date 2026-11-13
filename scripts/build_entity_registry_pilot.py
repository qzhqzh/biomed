"""Prepare local registry seed data; never connect to tumor_api or change facts."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "var/experiments/entity-resolution-20261101"
SNAPSHOT = ROOT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
MAIN_CANCERS = {
    "脑胶质瘤",
    "乳腺癌",
    "小细胞肺癌",
    "前列腺癌",
    "胆道肿瘤",
    "结直肠癌",
    "膀胱癌",
    "甲状腺癌",
}


def key(kind, identifier):
    return f"seed:tumor_api:{kind}:{identifier}"


def normalized(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def seed_payload(snapshot, snapshot_hash, *, exclude_placeholder_aliases=False):
    entities, names = [], defaultdict(list)
    tumors = {row["name_cn"]: row for row in snapshot["tumors"]}
    brain_key = key("disease", tumors["脑胶质瘤"]["id"])
    for collection, kind in (("drugs", "drug"), ("tumors", "disease")):
        for row in snapshot[collection]:
            entity_key = key(kind, row["id"])
            name = row.get("name_cn") or row.get("name") or row.get("name_en") or ""
            name_missing = not str(name).strip()
            if name_missing:
                name = f"源库未命名（{row['id'][:8]}）"
            level, main_key, basis = (
                "not_applicable" if kind == "drug" else "unassigned",
                None,
                "",
            )
            if kind == "disease" and name in MAIN_CANCERS:
                level = "main"
                basis = (
                    "用户明确的脑胶质瘤主癌种"
                    if name == "脑胶质瘤"
                    else "本批已采用的业务主癌种；可在本地调整"
                )
            if kind == "disease" and name.startswith("弥漫性中线胶质瘤"):
                level, main_key = "subtype", brain_key
                basis = "用户明确脑胶质瘤为主癌种、弥漫性中线胶质瘤为亚型；分子限定和部位仍独立保存"
            aliases = sorted(
                {
                    str(value).strip()
                    for value in [
                        row.get("name"),
                        row.get("name_cn"),
                        row.get("name_en"),
                        row.get("name_en_abbr"),
                        *row.get("aliases", []),
                    ]
                    if value and str(value).strip()
                }
            )
            provenance = {
                "bootstrap_snapshot_sha256": snapshot_hash,
                "source_name_cn": row.get("name_cn", ""),
                "source_name_en": row.get("name_en", ""),
                "source_aliases": aliases,
                "source_parent_edges": row.get("parents", []),
                "classification_basis": basis,
                "classification_assigned": bool(basis),
                "classification_origin": "user_instruction"
                if name == "脑胶质瘤" or name.startswith("弥漫性中线胶质瘤")
                else "project_policy"
                if basis
                else "unassigned",
                "source_form": "possible_combination"
                if kind == "drug"
                and re.search(r"[+/;；]", name + " " + row.get("name_en", ""))
                else "not_reclassified",
                "clinical_validity_assessed": False,
                "name_missing": name_missing,
            }
            if kind == "disease":
                provenance["is_blood_tumor"] = row.get("is_blood_tumor")
                provenance["oncology_branch_basis"] = (
                    "source_flag_not_new_classification"
                )
            entities.append(
                {
                    "key": entity_key,
                    "kind": kind,
                    "name": name,
                    "name_en": row.get("name_en", ""),
                    "level": level,
                    "main_key": main_key,
                    "status": "candidate" if name_missing else "active",
                    "source_status": "seed",
                    "external_ids": {"tumor_api": row["id"]},
                    "provenance": provenance,
                }
            )
            for alias in aliases:
                if exclude_placeholder_aliases and not any(
                    character.isalnum() for character in alias
                ):
                    # Preserve the original source field in provenance, but a
                    # punctuation placeholder cannot identify an entity.
                    continue
                names[kind, normalized(alias)].append((entity_key, alias))
    rules = []
    for (kind, _), occurrences in sorted(names.items()):
        identities = sorted({entity_key for entity_key, _ in occurrences})
        unique = len(identities) == 1
        rules.append(
            {
                "kind": kind,
                "alias": occurrences[0][1],
                "scope": {"type": "global", "key": ""},
                "canonical_key": identities[0] if unique else None,
                "action": "match",
                "status": "confirmed" if unique else "unresolved",
                "origin": "dictionary",
                "reason": "基础快照唯一名称，对齐身份不代表本轮临床或分类审核"
                if unique
                else "基础快照同名多个实体，保留冲突，不任选UUID",
                "alternatives": [{"entity_key": identity} for identity in identities]
                if not unique
                else [],
                "evidence": [
                    {
                        "source": "tumor_api_bootstrap_snapshot",
                        "sha256": snapshot_hash,
                        "source_ids": [
                            identity.rsplit(":", 1)[-1] for identity in identities
                        ],
                    }
                ],
            }
        )
    return {
        "schema_version": "biomed.entity-registry.v1",
        "batch_id": "entity-registry-bootstrap-20261101-v3"
        if exclude_placeholder_aliases
        else "entity-registry-bootstrap-20261101-v2",
        "source": {
            "kind": "bootstrap",
            "snapshot_sha256": snapshot_hash,
            "original_snapshot": snapshot["source"],
            "runtime_target_access": False,
            "policy": "One-time reference seed. Runtime resolution and expansion use local data only.",
        },
        "entities": entities,
        "rules": rules,
        "mentions": [],
    }


def freeze(path, payload):
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.read_text() != content:
            raise ValueError("Refusing to overwrite different registry inputs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--exclude-placeholder-aliases",
        action="store_true",
        help="Future initial seeds only: omit punctuation aliases and use a distinct v3 batch.",
    )
    args = parser.parse_args()
    data = args.snapshot.read_bytes()
    payload = seed_payload(
        json.loads(data),
        hashlib.sha256(data).hexdigest(),
        exclude_placeholder_aliases=args.exclude_placeholder_aliases,
    )
    if args.output is None:
        args.output = ARTIFACTS / (
            "bootstrap-v3.json"
            if args.exclude_placeholder_aliases
            else "bootstrap-v2.json"
        )
    freeze(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "entities": len(payload["entities"]),
                "entity_kinds": dict(
                    Counter(row["kind"] for row in payload["entities"])
                ),
                "rules": dict(Counter(row["status"] for row in payload["rules"])),
                "target_contacted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
