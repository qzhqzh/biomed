"""Append guarded local registry decisions from frozen evidence, never rewrite facts.

The historical strict experiment and its import remain reproducible. This batch
adopts the later business acceptance for cancer grouping and repairs four drug
references using existing source text and the existing ChEMBL snapshot only.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.light_entity_linking import _key
from scripts.build_entity_registry_literature import file_hash, read
from scripts.build_entity_registry_pilot import freeze, normalized
from scripts.light_entity_pilot import CHEMBL
from scripts.rescore_entity_resolution_business import rescore

VERSION = "entity-registry-operational-v2"
DEFAULT = ROOT / "var/experiments/entity-resolution-20261101"


def digest(value):
    # Same canonical serialization as entity_registry.digest, without Django setup.
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def checked_snapshot(row):
    snapshot = {k: v for k, v in row.items() if k != "state_sha256"}
    if digest(snapshot) != row.get("state_sha256"):
        raise ValueError("Registry state snapshot hash mismatch")
    return snapshot


def source_quotes(item):
    return [s["quote"] for m in item["mentions"] for s in m["source_support"]]


def retinoid_dictionary_match(label, quotes, molecules):
    """Expand only a documented RA abbreviation, preserving the full isomer name."""
    query = _key(label)
    if query in {"ra", "retinoic acid"}:
        return None  # A class/unspecified isomer is never a specific ingredient.
    if query.endswith(" ra"):
        if not any(re.search(r"retinoic acid\s*\(RA\)", q) for q in quotes):
            return None
        query = query[:-3] + " retinoic acid"
    matches = []
    for row in molecules:
        names = {row.get("pref_name", "")}
        names.update(
            synonym.get(field, "")
            for synonym in row.get("molecule_synonyms", [])
            for field in ("molecule_synonym", "synonyms")
        )
        matched = sorted(name for name in names if name and _key(name) == query)
        if matched:
            matches.append((row, matched))
    if len(matches) != 1:
        return None
    row, aliases = matches[0]
    return {
        "identifier": row["molecule_chembl_id"],
        "label": row["pref_name"],
        "matched_aliases": aliases,
        "expanded_query": query,
        "source": row.get("_source", {}),
    }


def build(
    root, state, chembl_path=CHEMBL, *, expected_references=18, expected_rules=10
):
    business = read(root / "comparison-business-v2.json")
    if business != rescore(root):
        raise ValueError("Business comparison differs from the frozen raw outputs")
    if business["selection"]["model"] != "bonsai2-27b":
        raise ValueError("Expected the selected frozen Bonsai normalization arm")
    entities = {e["key"]: checked_snapshot(e) for e in state["entities"]}
    entities_by_id = {e["id"]: e for e in entities.values()}
    rules = [checked_snapshot(r) for r in state["rules"]]
    items = {r["id"]: r for r in read(root / "items.json")["records"]}
    evaluations = {
        r["id"]: r for r in business["arms"]["bonsai2-27b"]["disease"]["records"]
    }
    proposed = state["proposed_mentions"]
    selected_rule_ids = {m["applied_rule_id"] for m in proposed}
    selected_rules = [r for r in rules if r["id"] in selected_rule_ids]
    if len(proposed) != expected_references or len(selected_rules) != expected_rules:
        raise ValueError(
            "Registry backlog differs from the requested frozen repair scope"
        )
    molecules = read(chembl_path)
    if isinstance(molecules, dict):
        molecules = molecules.get("molecules", molecules.get("records"))
    if not isinstance(molecules, list):
        raise TypeError("Expected existing local ChEMBL molecules")
    chembl_sha = file_hash(chembl_path)
    updates, additions, new_entities, activations, classifications = [], [], [], {}, {}
    existing_identities = {
        (r["kind"], normalized(r["alias"]), r["scope"]["type"], r["scope"]["key"])
        for r in rules
    }

    def activate(key, *, level=None):
        entity = entities[key]
        if entity["status"] == "active":
            return
        activations[key] = {
            "entity_key": key,
            "expected_revision": entity["revision"],
            "expected_content_sha256": digest(entity),
            "origin": "rule",
            "reason": "现有原文及本地字典支持，复用现有候选实体；不生成第二份身份。",
            "provenance_patch": {"entity_level": level} if level else {},
        }

    def reusable(rule, alias=None):
        alias = alias or rule["alias"]
        identity = (rule["kind"], normalized(alias), "global", "")
        if identity in existing_identities:
            return  # Never change an existing confirmed or conflicting global rule.
        value = {
            k: copy.deepcopy(v)
            for k, v in rule.items()
            if not k.startswith("expected_")
        }
        value.update(alias=alias, scope={"type": "global", "key": ""})
        value["provenance"]["reused_from_article_rule"] = rule["scope"]
        additions.append(value)
        existing_identities.add(identity)

    for old in selected_rules:
        if old["status"] != "proposed" or old["origin"] == "human":
            raise ValueError("Only untouched proposed system decisions may be adopted")
        item_id = old["provenance"]["item_id"]
        item = items[item_id]
        if item["label"] != old["alias"] or item["kind"] != old["kind"]:
            raise ValueError("Source item no longer matches the proposed rule")
        evidence = copy.deepcopy(old["evidence"])
        provenance = {
            "version": VERSION,
            "item_id": item_id,
            "human_confirmed": False,
            "previous_rule_sha256": digest(old),
            "original_facts_modified": False,
        }
        required = []
        if item["kind"] == "disease":
            evaluation = evaluations[item_id]
            if (
                not evaluation["business_correct"]
                or not evaluation["direct_main_correct"]
            ):
                raise ValueError(
                    "Do not adopt an incorrect or explanation-only main cancer"
                )
            prediction = evaluation["prediction"]
            main = prediction["main_cancer_id"]
            subtype = prediction.get("subtype_candidate_id")
            key = subtype or main
            if main not in entities or entities[main]["level"] != "main":
                raise ValueError(
                    "Main cancer must be an existing classified local entity"
                )
            if subtype:
                target = entities[subtype]
                if target["level"] == "unassigned":
                    classifications[subtype] = {
                        "entity_key": subtype,
                        "level": "subtype",
                        "main_key": main,
                        "expected_revision": target["revision"],
                        "origin": "model",
                        "reason": "Bonsai与冻结参考一致：三阴乳腺癌属于乳腺癌亚型。",
                        "evidence": [
                            {
                                "comparison_sha256": file_hash(
                                    root / "comparison-business-v2.json"
                                )
                            }
                        ],
                    }
                elif (
                    target["level"] != "subtype"
                    or entities_by_id[target["main_id"]]["key"] != main
                ):
                    raise ValueError(
                        "Existing subtype classification conflicts with the adopted answer"
                    )
            else:
                # Label-level distinctions only. Do not attach background treatment
                # qualifiers from a model's context list to every source mention.
                required = {
                    "E004": ["Differentiated thyroid cancer"],
                    "E007": ["Differentiated thyroid cancer"],
                    "E011": ["limited-stage"],
                    "E014": ["metastatic", "castration-resistant"],
                }[item_id]
                same_article = [
                    s["quote"]
                    for r in items.values()
                    for m in r["mentions"]
                    if str(uuid.UUID(m["version_id"])) == old["scope"]["key"]
                    for s in m["source_support"]
                ]
                if not all(any(q in text for text in same_article) for q in required):
                    raise ValueError(
                        "A retained disease qualifier lacks source support"
                    )
            action, origin = "alias", "model"
            reason = "采用已冻结Bonsai结果：主癌种正确即接受主类对应，亚型与限定另存，不扩张原知识适用人群。"
            provenance.update(
                method="business_main_acceptance",
                model="bonsai2-27b",
                main_cancer_key=main,
                raw_prediction=prediction,
                extended_fields=evaluation["extended_fields"],
                comparison_sha256=file_hash(root / "comparison-business-v2.json"),
                match_relation="qualified_form" if required else "alias",
                required_qualifiers=required,
            )
        elif item_id == "E022":
            quotes = source_quotes(item) + source_quotes(items["E040"])
            match = retinoid_dictionary_match(item["label"], quotes, molecules)
            if not match or match["identifier"] != "CHEMBL705":
                raise ValueError(
                    "9-cis RA must be supported by the existing ChEMBL705 synonym"
                )
            possible = [
                e["key"]
                for e in entities.values()
                if e["kind"] == "drug"
                and e["external_ids"].get("CHEMBL") == match["identifier"]
            ]
            if len(possible) != 1:
                raise ValueError(
                    "The ChEMBL ingredient must have one existing local canonical identity"
                )
            key = possible[0]
            activate(key, level="drug_ingredient")
            action, origin = "alias", "dictionary"
            reason = "原文定义retinoic acid (RA)；保留9-cis异构体，经本地ChEMBL705完整同义词对应alitretinoin。"
            provenance.update(
                method="source_abbreviation_and_local_isomer_alias",
                dictionary_match=match,
                dictionary_sha256=chembl_sha,
                match_relation="alias",
            )
        elif item_id == "E035":
            if not any(
                "CYP26A1 inhibitors Liarozole and Talarozole" in q
                for q in source_quotes(item)
            ):
                raise ValueError(
                    "Liarozole drug registration lacks the explicit source role"
                )
            key = (
                "literature:drug:"
                + hashlib.sha256(normalized(item["label"]).encode()).hexdigest()[:24]
            )
            if key not in entities:
                new_entities.append(
                    {
                        "key": key,
                        "kind": "drug",
                        "name": "Liarozole",
                        "name_en": "Liarozole",
                        "level": "not_applicable",
                        "main_key": None,
                        "status": "active",
                        "source_status": "literature_new",
                        "external_ids": {},
                        "provenance": {
                            "entity_level": "drug_ingredient",
                            "source_support": evidence,
                            "registration_basis": "source_explicit_named_CYP26A1_inhibitor",
                            "clinical_validity_assessed": False,
                            "not_a_claim_of_new_molecule_or_approval": True,
                        },
                    }
                )
            action, origin = "new", "rule"
            reason = "原文明确Liarozole为CYP26A1抑制剂，登记本地药物；缺外部ID不等于不是药物。"
            provenance.update(
                method="source_explicit_named_drug", match_relation="new_entity"
            )
        elif item_id in {"E040", "E041"}:
            if not any("retinoic acid (RA) agents" in q for q in source_quotes(item)):
                raise ValueError(
                    "The source must explicitly define the generic RA agents"
                )
            existing = next(
                r for r in selected_rules if r["provenance"]["item_id"] == "E041"
            )
            key = entities_by_id[existing["canonical_id"]]["key"]
            if entities[key]["external_ids"]:
                raise ValueError(
                    "Generic RA must not inherit a specific ingredient identifier"
                )
            activate(key, level="drug_class")
            action, origin = "alias", "rule"
            reason = "本篇RA与retinoic acid合并为未指明异构体的视黄酸类；不对应tretinoin、alitretinoin或isotretinoin。"
            provenance.update(
                method="article_defined_unspecified_isomer_class",
                match_relation="alias",
                entity_level="drug_class",
                specific_ingredient_id=None,
            )
        else:
            raise ValueError(f"Unexpected unresolved source item: {item_id}")
        rule = {
            "kind": old["kind"],
            "alias": old["alias"],
            "scope": old["scope"],
            "canonical_key": key,
            "action": action,
            "status": "confirmed",
            "origin": origin,
            "reason": reason,
            "alternatives": [],
            "evidence": evidence,
            "provenance": provenance,
            "expected_revision": old["revision"],
            "expected_content_sha256": digest(old),
        }
        updates.append(rule)
        if item_id in {"E004", "E011", "E014", "E021", "E035"}:
            reusable(rule)
        if item_id == "E022":
            for alias in ("9-cis retinoic acid", "9-cis-retinoic acid", "alitretinoin"):
                reusable(rule, alias)
    payload = {
        "schema_version": "biomed.entity-registry.v1",
        "batch_id": "entity-registry-operational-20261101-v2",
        "source": {
            "kind": "literature",
            "version": VERSION,
            "selected_model": "bonsai2-27b",
            "registry_state_sha256": digest(state),
            "new_model_calls": 0,
            "comparison_sha256": file_hash(root / "comparison-business-v2.json"),
            "chembl_sha256": chembl_sha,
            "target_contacted": False,
            "original_facts_modified": False,
            "expected_resolved_references": len(proposed),
            "disease_references": sum(
                m["applied_rule_id"]
                in {r["id"] for r in selected_rules if r["kind"] == "disease"}
                for m in proposed
            ),
            "drug_references": sum(
                m["applied_rule_id"]
                in {r["id"] for r in selected_rules if r["kind"] == "drug"}
                for m in proposed
            ),
            "drug_class_is_not_ingredient": True,
        },
        "entities": new_entities,
        "rules": additions,
        "mentions": [],
        "classifications": list(classifications.values()),
        "rule_updates": updates,
        "new_candidates_activation": list(activations.values()),
    }
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--chembl", type=Path, default=CHEMBL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build(args.root, read(args.state), args.chembl)
    freeze(args.output, payload)
    print(
        json.dumps(
            {
                "path": str(args.output),
                **{
                    key: len(payload[key])
                    for key in (
                        "entities",
                        "rules",
                        "rule_updates",
                        "new_candidates_activation",
                        "classifications",
                    )
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
