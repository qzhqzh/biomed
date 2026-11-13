"""Extend a frozen pilot with reusable, self-contained qualified disease names.

This only prepares a new local batch. Original article-scoped rules, source
facts, frozen model results and the already imported batch remain unchanged.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_entity_registry_pilot import freeze

DEFAULT = (
    Path(__file__).resolve().parents[1] / "var/experiments/entity-resolution-20261101"
)
POLICY = "qualified-complete-name-v1"


def normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def content_hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def can_reuse_qualified_rule(rule, existing_global_rules=()):
    """Only complete, qualified names can leave their original article scope."""
    provenance = rule.get("provenance", {})
    if (
        rule.get("status") != "confirmed"
        or rule.get("kind") != "disease"
        or not rule.get("canonical_key")
    ):
        return False
    if provenance.get("match_relation") != "qualified_form":
        return False
    alias = rule["alias"]
    name = normalized(alias)
    # Conservatively keep abbreviations/context labels local. The one molecular
    # DMG name is explicitly covered by the user's fixed business definition.
    complete_name = (
        bool(
            re.search(
                r"癌|瘤|(?:cancer|carcinoma|sarcoma|melanoma|glioma|mesothelioma|tumou?r|blastoma)\b",
                name,
            )
        )
        or name == "h3 k27m-mutant dmg"
    )
    if len(name) <= 5 or not complete_name:
        return False
    required = provenance.get("required_qualifiers")
    if (
        not isinstance(required, list)
        or not required
        or any(
            not isinstance(q, str) or not normalized(q) or normalized(q) not in name
            for q in required
        )
    ):
        return False
    return not any(
        r.get("scope", {}).get("type", "global") == "global"
        and r.get("kind") == "disease"
        and normalized(r.get("alias", "")) == name
        for r in existing_global_rules
    )


def build(root):
    root = Path(root)
    source_path = root / "literature-registry.json"
    seed_path = root / "bootstrap-v2.json"
    source_bytes, seed_bytes = source_path.read_bytes(), seed_path.read_bytes()
    source, seed = json.loads(source_bytes), json.loads(seed_bytes)
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    existing_global = seed["rules"] + [
        r for r in source["rules"] if r["scope"]["type"] == "global"
    ]
    rules = {}
    for original in source["rules"]:
        if original["scope"]["type"] != "article" or not can_reuse_qualified_rule(
            original, existing_global
        ):
            continue
        identity = normalized(original["alias"])
        reference = {
            "source_batch_id": source["batch_id"],
            "source_batch_sha256": source_sha,
            "source_rule_sha256": content_hash(original),
            "source_rule_identity": {
                k: original[k] for k in ["kind", "alias", "scope", "canonical_key"]
            },
        }
        if identity in rules:
            previous = rules[identity]
            if (
                previous["canonical_key"] != original["canonical_key"]
                or previous["provenance"]["required_qualifiers"]
                != original["provenance"]["required_qualifiers"]
            ):
                raise ValueError(
                    "Conflicting article decisions cannot become one global rule"
                )
            previous["provenance"]["source_rules"].append(reference)
            continue
        rule = copy.deepcopy(original)
        rule["scope"] = {"type": "global", "key": ""}
        rule["reason"] += (
            "；完整疾病名已包含全部必要限定，可跨文章复用；限定继续随每次关联保存。"
        )
        rule["provenance"].update(
            reuse_policy=POLICY,
            source_rules=[reference],
            reuse_requires_exact_complete_name=True,
        )
        rules[identity] = rule
    return {
        "schema_version": "biomed.entity-registry.v1",
        "batch_id": "entity-registry-qualified-reuse-20261101-v1",
        "source": {
            "kind": "literature",
            "policy": POLICY,
            "source_batch_id": source["batch_id"],
            "source_batch_sha256": source_sha,
            "bootstrap_sha256": hashlib.sha256(seed_bytes).hexdigest(),
            "target_contacted": False,
            "original_facts_modified": False,
        },
        "entities": [],
        "mentions": [],
        "classifications": [],
        "rules": list(rules.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = build(args.root)
    output = args.output or args.root / "qualified-reuse-registry.json"
    freeze(output, payload)
    print(
        json.dumps(
            {
                "path": str(output),
                "batch_id": payload["batch_id"],
                "global_rules": len(payload["rules"]),
                "aliases": [r["alias"] for r in payload["rules"]],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
