"""Build a local, reviewable registry batch from the fixed 30-knowledge pilot.

No target connection, model call, or source-fact mutation. Independent model
assessment controls whether a proposal is reusable, not whether a fact exists.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_entity_registry_pilot import freeze, normalized

DEFAULT = ROOT / "var/experiments/entity-resolution-20261101"


def read(path):
    return json.loads(path.read_text())


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root, *, expected_records=30, expected_mentions=89):
    bootstrap = read(root / "bootstrap-v2.json")
    manifest = read(root / "manifest.json")
    if (
        manifest.get("source_records") != expected_records
        or manifest.get("participating_mentions") != expected_mentions
    ):
        raise ValueError("Frozen source coverage does not match the requested pilot")
    comparison = read(root / "comparison.json")
    chosen = comparison["selection"]["model"]
    evaluations = {r["id"]: r for r in comparison["arms"][chosen]["records"]}
    reference = {r["id"]: r for r in read(root / "reference.json")["records"]}
    catalog = {r["id"]: r for r in read(root / "candidate-catalog.json")["records"]}
    seeds = {r["key"]: r for r in bootstrap["entities"]}
    seed_rules = {(r["kind"], normalized(r["alias"])): r for r in bootstrap["rules"]}
    entities, rules, mentions, classifications = {}, {}, {}, {}

    # Reference-equivalent IDs are collapsed only after a correct model choice.
    # This prevents duplicate RxNorm/ChEMBL identities, without repairing errors.
    equivalent = {}
    for ref in reference.values():
        ids = [a["candidate_id"] for a in ref["accepted"] if a["candidate_id"]]
        if ids:
            preferred = next(
                (c for c in ids if c.startswith("seed:")),
                next((c for c in ids if c.startswith("std:RXNORM:")), ids[0]),
            )
            equivalent.update({c: preferred for c in ids})
    equivalent.update({"std:CHEMBL:CHEMBL547": "std:RXNORM:RXNORM:6064"})

    def register(candidate, kind, *, active=True, label=None, main=None, subtype=False):
        candidate = equivalent.get(candidate, candidate)
        if candidate in seeds:
            if seeds[candidate]["kind"] != kind:
                raise ValueError("Same seed identity cannot cross entity kinds")
            if subtype and seeds[candidate]["level"] == "unassigned":
                classifications[candidate] = {
                    "entity_key": candidate,
                    "level": "subtype",
                    "main_key": main,
                    "expected_revision": 1,
                    "origin": "model",
                    "reason": "本批模型与预先冻结的独立参考一致；仅补充未指定的业务层级。",
                    "evidence": [
                        {
                            "comparison_sha256": file_hash(root / "comparison.json"),
                            "model": chosen,
                        }
                    ],
                }
            return candidate
        if candidate in entities:
            existing = entities[candidate]
            if existing["kind"] != kind:
                raise ValueError("Same canonical identity cannot cross entity kinds")
            # A candidate may first appear as an unconfirmed distractor. A later
            # independently passed resolution can activate that same identity.
            if active:
                existing["status"] = "active"
                if subtype:
                    if existing["level"] not in {"unassigned", "subtype"} or (
                        existing["level"] == "subtype" and existing["main_key"] != main
                    ):
                        raise ValueError("Conflicting confirmed subtype classification")
                    existing.update(level="subtype", main_key=main)
                    existing["provenance"]["classification_origin"] = "model"
            return candidate
        detail = catalog.get(candidate, {})
        standard_name = detail.get("label") or label
        if not standard_name:
            raise ValueError(f"Missing name for {candidate}")
        external_ids = (
            {detail["namespace"]: detail["identifier"]}
            if detail.get("identifier")
            else {}
        )
        equivalent_ids = sorted(
            {
                candidate,
                *(key for key, value in equivalent.items() if value == candidate),
            }
        )
        for identity in equivalent_ids:
            other = catalog.get(identity, {})
            if other.get("identifier") and other.get("namespace") not in external_ids:
                external_ids[other["namespace"]] = other["identifier"]
        entities[candidate] = {
            "key": candidate,
            "kind": kind,
            "name": standard_name,
            "name_en": standard_name,
            "level": "not_applicable"
            if kind == "drug"
            else "subtype"
            if subtype
            else "unassigned",
            "main_key": main if subtype else None,
            "status": "active" if active else "candidate",
            "source_status": "literature_new",
            "external_ids": external_ids,
            "provenance": {
                "local_standard": detail,
                "new_to_local_registry": True,
                "clinical_validity_assessed": False,
                "not_a_claim_of_new_molecule_or_approval": True,
                "equivalent_local_candidate_ids": equivalent_ids,
                "classification_origin": "model" if subtype else "unassigned",
            },
        }
        return candidate

    def add(row, result, *, passed, origin, method, assessment=None):
        label, kind = row["label"], row["kind"]
        decision, target = result["decision"], result.get("candidate_id")
        match_relation = result.get("match_relation", "alias")
        qualifiers = result.get("qualifiers", [])
        if label == "H3 K27M-mutant DMG":
            match_relation, qualifiers = "qualified_form", ["H3 K27M-mutant"]
        if passed and match_relation == "qualified_form" and not qualifiers:
            raise ValueError(
                "Confirmed qualified disease mapping lost its source conditions"
            )
        main = result.get("main_cancer_id")
        subtype = result.get("subtype_candidate_id")
        canonical = None
        if target and (passed or (assessment or {}).get("identity_correct")):
            canonical = register(
                target,
                kind,
                active=passed,
                label=label,
                main=main,
                subtype=bool(subtype and passed),
            )
        elif decision == "NEW":
            identity = hashlib.sha256(normalized(label).encode()).hexdigest()[:24]
            canonical = register(
                f"literature:{kind}:{identity}", kind, active=passed, label=label
            )
        status = (
            "confirmed"
            if passed and decision in {"MATCH", "NEW", "NOT_ENTITY"}
            else "unresolved"
            if decision == "UNRESOLVED"
            else "proposed"
        )
        action = (
            "ignore"
            if decision == "NOT_ENTITY"
            else "new"
            if decision == "NEW"
            else "alias"
        )
        reason = result.get("reason", "本地规则对齐。")
        if assessment and not passed:
            reason += "；独立参考未完整通过，保留待审，不自动应用。"
        options = []
        if status != "confirmed":
            # Present local candidate identities without inventing target IDs.
            for candidate in row.get("candidates", []):
                option = equivalent.get(candidate["id"], candidate["id"])
                if option == canonical or any(
                    o["entity_key"] == option for o in options
                ):
                    continue
                option = register(option, kind, active=False, label=candidate["label"])
                options.append({"entity_key": option, "label": candidate["label"]})
        source_mentions = row["mentions"]
        short_context_name = kind == "disease" and (label.isupper() or len(label) <= 5)
        prior = seed_rules.get((kind, normalized(label)))
        reuse_seed = (
            prior
            and prior["status"] == "confirmed"
            and prior["canonical_key"] == canonical
            and status == "confirmed"
            and match_relation != "qualified_form"
        )
        scoped = bool(
            short_context_name
            or prior
            and not reuse_seed
            or match_relation == "qualified_form"
            or status != "confirmed"
        )
        scopes = sorted({m["version_id"] for m in source_mentions}) if scoped else [""]
        evidence = []
        for mention in source_mentions:
            for support in mention.get("source_support", []):
                if support not in evidence:
                    evidence.append(support)
            ident = (mention["source_run"], mention["group_id"], mention["entity_ref"])
            if ident in mentions:
                raise ValueError("Duplicate participating mention")
            mentions[ident] = {
                "source_run": ident[0],
                "group_id": ident[1],
                "entity_ref": ident[2],
                "qualifiers": {
                    "original_label": label,
                    "source_support": mention.get("source_support", []),
                    "source_sha256": mention["source_sha256"],
                    "source_qualifiers": qualifiers,
                    "resolution_item": row["id"],
                },
            }
        if reuse_seed:
            return
        for scope in scopes:
            identity = (kind, normalized(label), scope)
            if identity in rules:
                raise ValueError("Conflicting pilot alias")
            rules[identity] = {
                "kind": kind,
                "alias": label,
                "scope": {"type": "article" if scope else "global", "key": scope},
                "canonical_key": canonical,
                "action": action,
                "status": status,
                "origin": origin,
                "reason": reason,
                "alternatives": options,
                "evidence": evidence,
                "provenance": {
                    "item_id": row["id"],
                    "method": method,
                    "model": chosen if origin == "model" else None,
                    "match_relation": match_relation,
                    "required_qualifiers": qualifiers
                    if match_relation == "qualified_form"
                    else [],
                    "independent_reference_passed": passed
                    if origin == "model"
                    else None,
                    "human_confirmed": False,
                    "reference_sha256": file_hash(root / "reference.json")
                    if origin == "model"
                    else None,
                    "raw_prediction": result if origin == "model" else None,
                    "assessment": {
                        k: v
                        for k, v in (assessment or {}).items()
                        if k
                        in {
                            "identity_correct",
                            "classification_correct",
                            "qualifiers_correct",
                            "protocol_valid",
                        }
                    },
                },
            }

    for row in read(root / "rules.json")["records"]:
        result = row["resolution"]
        origin = (
            "user_instruction"
            if result["method"] == "user_business_group_and_source_definition"
            else "rule"
        )
        add(row, result, passed=True, origin=origin, method=result["method"])
    for row in read(root / "items.json")["records"]:
        if row["cohort"] != "unresolved_after_rules":
            continue
        evaluation = evaluations[row["id"]]
        prediction = evaluation["prediction"]
        if (
            not isinstance(prediction, dict)
            or not isinstance(prediction.get("decision"), str)
            or prediction.get("decision")
            not in {
                "MATCH",
                "NEW",
                "UNRESOLVED",
                "NOT_ENTITY",
            }
        ):
            prediction = {
                "decision": "UNRESOLVED",
                "candidate_id": None,
                "match_relation": "unresolved",
                "reason": "模型没有合法输出。",
            }
        add(
            row,
            prediction,
            passed=evaluation["all_correct"],
            origin="model",
            method=chosen,
            assessment=evaluation,
        )
    if len(mentions) != expected_mentions:
        raise ValueError(
            f"Expected {expected_mentions} participating mentions, got {len(mentions)}"
        )
    participating_candidates = len(
        {(m["source_run"], m["group_id"]) for m in mentions.values()}
    )
    if participating_candidates > expected_records:
        raise ValueError("Participating candidate count exceeds the frozen source set")
    payload = {
        "schema_version": "biomed.entity-registry.v1",
        "batch_id": "entity-registry-literature-20261101-v1",
        "source": {
            "kind": "literature",
            "selected_model": chosen,
            "inputs": {
                f: file_hash(root / f)
                for f in [
                    "bootstrap-v2.json",
                    "manifest.json",
                    "candidate-catalog.json",
                    "rules.json",
                    "items.json",
                    "reference.json",
                    "comparison.json",
                ]
            },
            "coverage": {
                "source_records": expected_records,
                "participating_mentions": len(mentions),
                "participating_candidates": participating_candidates,
            },
            "target_contacted": False,
            "original_facts_modified": False,
        },
        "entities": list(entities.values()),
        "rules": list(rules.values()),
        "mentions": list(mentions.values()),
        "classifications": list(classifications.values()),
    }
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT)
    args = parser.parse_args()
    payload = build(args.root)
    output = args.root / "literature-registry.json"
    freeze(output, payload)
    print(
        json.dumps(
            {
                "path": str(output),
                "model": payload["source"]["selected_model"],
                "entities": len(payload["entities"]),
                "mentions": len(payload["mentions"]),
                "rules": dict(Counter(r["status"] for r in payload["rules"])),
                "classifications": len(payload["classifications"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
