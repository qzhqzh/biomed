"""Rescore existing outputs under the user's revised main-cancer acceptance.

This is a post-hoc acceptance change, not a new model experiment. Frozen model
outputs, original reference and original comparison are never rewritten.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from scripts.benchmark_entity_resolution import aggregate, check_reference
from scripts.prepare_entity_resolution import DEFAULT, freeze, read

VERSION = "entity-resolution-business-v2"
ARMS = ("qwen35-9b", "bonsai2-27b")


def explicit_main_map(items, reference):
    """Only frozen reference IDs imply a main cancer; never infer from names."""
    mapping, evidence = {}, {}
    for item, expected in zip(items, reference["records"], strict=True):
        if item["kind"] != "disease":
            continue
        mains = {a["main_cancer_id"] for a in expected["accepted"]}
        if len(mains) != 1 or None in mains:
            raise ValueError(f"Reference has no unique business main cancer: {item['id']}")
        main = next(iter(mains))
        for answer in expected["accepted"]:
            for candidate in (main, answer["candidate_id"], answer["subtype_candidate_id"]):
                if candidate is None:
                    continue
                if candidate in mapping and mapping[candidate] != main:
                    raise ValueError(f"Conflicting frozen main mapping for {candidate}")
                mapping[candidate] = main
                evidence.setdefault(candidate, set()).add(item["id"])
    return mapping, {key: sorted(value) for key, value in evidence.items()}


def score_disease(item, reference, old, mapping):
    value = old.get("prediction")
    value = value if isinstance(value, dict) else {}
    expected_main = reference["accepted"][0]["main_cancer_id"]
    main = value.get("main_cancer_id")
    candidate = value.get("candidate_id")
    subtype = value.get("subtype_candidate_id")
    candidates = {c["id"] for c in item["candidates"]}
    allowed_main = {c["id"] for c in item["main_cancer_candidates"]}
    candidate_main = mapping.get(candidate) if isinstance(candidate, str) else None
    subtype_main = mapping.get(subtype) if isinstance(subtype, str) else None
    failures = []
    if old.get("raw_status") != "classified":
        failures.append("model_output_unavailable")
    decision = value.get("decision")
    if decision == "NOT_ENTITY":
        failures.append("disease_rejected_as_non_entity")
    elif decision not in ("MATCH", "NEW", "UNRESOLVED"):
        failures.append("invalid_decision")
    if main is not None:
        if not isinstance(main, str) or main not in allowed_main:
            failures.append("invalid_main_id")
        elif main != expected_main:
            failures.append("wrong_main_cancer")
    if candidate is not None:
        if not isinstance(candidate, str) or candidate not in candidates:
            failures.append("candidate_not_in_frozen_choices")
        elif candidate_main is not None and candidate_main != expected_main:
            failures.append("candidate_points_to_wrong_main")
    if candidate_main is not None and main is not None and candidate_main != main:
        failures.append("main_candidate_conflict")
    if subtype_main is not None and subtype_main != expected_main:
        failures.append("subtype_main_conflict")

    direct = main == expected_main and not failures
    # A known candidate can repair an omitted main field, but never overwrite a
    # wrong explicit main or rescue NOT_ENTITY. It is not model direct accuracy.
    recovered = main is None and candidate_main == expected_main and not failures
    if main is None and not recovered:
        failures.append("missing_structured_main")
    reason = value.get("reason") if isinstance(value.get("reason"), str) else ""
    mention_in_reason = expected_main in reason
    subtype_match = any(value.get("subtype_candidate_id") == a["subtype_candidate_id"]
                        for a in reference["accepted"])
    subtype_required = any(a["subtype_candidate_id"] is not None for a in reference["accepted"])
    qualifier_required = bool(reference.get("required_qualifiers"))
    return {
        "id": item["id"], "label": item["label"], "kind": item["kind"], "cohort": item["cohort"],
        "expected_main_id": expected_main, "reported_main_id": main,
        "candidate_id": candidate, "candidate_main_id": candidate_main,
        "direct_main_correct": direct, "script_recovered": recovered,
        "business_correct": direct or recovered, "failures": failures,
        "explanation_mentions_expected_main_id": mention_in_reason,
        "explanation_only_main_id_mention": main is None and not recovered and mention_in_reason,
        "explanation_note": "Expected main ID occurs in explanation but is not counted as a structured answer or recovery."
                            if main is None and not recovered and mention_in_reason else "",
        "extended_fields": {
            "subtype_matches_reference": subtype_match,
            "subtype_required": subtype_required,
            "required_qualifiers_present": bool(old["qualifiers_correct"]),
            "qualifier_requirement_present": qualifier_required,
            "source_protocol_valid": bool(old["protocol_valid"]),
            "extension_fields_complete": subtype_match and bool(old["qualifiers_correct"]) and bool(old["protocol_valid"]),
            "complete_under_original_reference": bool(old["all_correct"]),
            "missing_qualifiers": old.get("missing_qualifiers", []),
        },
        "original_identity_correct": old["identity_correct"],
        "prediction": value, "seconds": old["seconds"],
    }


def summarize_disease(rows):
    count = len(rows)
    fields = ("subtype_matches_reference", "required_qualifiers_present",
              "source_protocol_valid", "extension_fields_complete", "complete_under_original_reference")
    extensions = {key: sum(r["extended_fields"][key] for r in rows) for key in fields}
    # Conditional denominators prevent empty subtype/qualifier fields from
    # appearing as successful completion of a medically meaningful field.
    for field, required in (("subtype_matches_reference", "subtype_required"),
                            ("required_qualifiers_present", "qualifier_requirement_present")):
        applicable = [r for r in rows if r["extended_fields"][required]]
        extensions[field + "_applicable"] = len(applicable)
        extensions[field + "_applicable_correct"] = sum(r["extended_fields"][field] for r in applicable)
    direct = sum(r["direct_main_correct"] for r in rows)
    return {
        "items": count, "direct_main_correct": direct,
        "direct_main_accuracy": round(direct / count, 6) if count else None,
        "script_recovered": sum(r["script_recovered"] for r in rows),
        "accepted_with_recovery": sum(r["business_correct"] for r in rows),
        "failed": sum(not r["business_correct"] for r in rows),
        "explanation_only_main_id_mentions": sum(r["explanation_only_main_id_mention"] for r in rows),
        "extended_fields": extensions,
        "failure_ids": [r["id"] for r in rows if not r["business_correct"]],
        "records": rows,
    }


def validate_outputs(root, inputs, items, comparison):
    for arm in ARMS:
        old_rows = comparison["arms"][arm]["records"]
        if [r["id"] for r in old_rows] != [r["id"] for r in items]:
            raise ValueError("Original comparison item order mismatch")
        for source, old in zip(inputs["records"], old_rows, strict=True):
            result = read(root / arm / "extract" / f"result-{old['id']}.json")
            if (result["id"] != source["id"] or result["input_sha256"] != source["input_sha256"]
                    or (result.get("payload") or {}) != old["prediction"]
                    or result["status"] != old["raw_status"] or result["seconds"] != old["seconds"]):
                raise ValueError(f"Original output/comparison mismatch: {arm} {old['id']}")


def rescore(root):
    reference, comparison = read(root / "reference.json"), read(root / "comparison.json")
    inputs, items = check_reference(root, reference)
    if comparison["reference_sha256"] != digest(reference):
        raise ValueError("Original comparison reference mismatch")
    validate_outputs(root, inputs, items, comparison)
    mapping, mapping_evidence = explicit_main_map(items, reference)
    arms = {}
    for arm in ARMS:
        old = comparison["arms"][arm]
        disease, drugs, challenge = [], [], []
        for item, expected, row in zip(items, reference["records"], old["records"], strict=True):
            if item["cohort"] == "known_risky_synonym_challenge":
                challenge.append(score_disease(item, expected, row, mapping))
            elif item["kind"] == "disease":
                disease.append(score_disease(item, expected, row, mapping))
            elif item["kind"] == "drug":
                drugs.append(row)
            else:
                raise ValueError("Unexpected frozen entity kind")
        if (len(disease), len(drugs), len(challenge)) != (14, 5, 1):
            raise ValueError("Frozen pilot cohort sizes changed")
        summary = old["inference_summary"]
        arms[arm] = {
            "disease": summarize_disease(disease),
            "drugs": {**aggregate(drugs), "records": drugs, "acceptance_changed": False},
            "challenge": summarize_disease(challenge),
            "resources": {
                "disease_warm_seconds": round(sum(r["seconds"] for r in disease), 6),
                "drug_warm_seconds": round(sum(r["seconds"] for r in drugs), 6),
                "challenge_warm_seconds": round(sum(r["seconds"] for r in challenge), 6),
                "all_warm_seconds": old["warm_seconds"],
                "nvml_process_peak_mib": summary["nvml_process_peak_mib"],
                "load_seconds": summary.get("load_seconds"),
                "warmup_seconds": summary.get("warmup_seconds"),
                "original_sessions": summary.get("sessions", []),
                "new_measurement": False,
            },
        }

    def rank(name):
        a = arms[name]
        return (-a["disease"]["direct_main_correct"], -a["disease"]["accepted_with_recovery"],
                -a["drugs"]["identity_correct"], a["resources"]["all_warm_seconds"])

    selected = min(arms, key=rank)
    return {
        "version": VERSION,
        "post_hoc_acceptance_change": True, "no_new_inference": True,
        "acceptance_change": "User clarified that a correct main cancer is sufficient for disease acceptance; subtype and stage/treatment-state qualifiers are information-completeness checks. Drug identity criteria are unchanged.",
        "provenance": {
            "hash_method": "apps.curation.decision.digest: sorted-key UTF-8 JSON",
            "comparison_sha256": digest(comparison), "reference_sha256": digest(reference),
            "inputs_sha256": digest(inputs), "items_sha256": digest(read(root / "items.json")),
            "original_selection": comparison["selection"]["model"],
            "source_root": str(root), "original_outputs_verified": True,
            "original_artifacts_rewritten": False,
        },
        "policy": {
            "main_cancer_direct": "A supplied, correct main_cancer_id; NEW/UNRESOLVED and missing duplicate subtype fields do not invalidate it. NOT_ENTITY, conflicting main cancer IDs and invented candidate IDs are rejected.",
            "script_recovery": "Only a supplied candidate_id with an explicit frozen reference ID-to-main mapping can recover an omitted main field. Explanation text is never used for recovery.",
            "extended_fields": "Shown separately; not disease main-cancer acceptance gates.",
            "drugs": "Original decision, candidate ID and match relation acceptance unchanged; isomer identity is not relaxed.",
            "evaluation_scope": "Post-hoc reassessment of 14 disease terms and 5 drug terms in a development set; the one DMG challenge is separate. No new generalization claim.",
        },
        "explicit_candidate_main_map": mapping, "mapping_reference_items": mapping_evidence,
        "selection": {
            "model": selected, "scope": "This local entity-normalization stage under the revised business acceptance",
            "rule": "disease direct main correct, then main correct including explicit script recovery, then unchanged drug identity, then original total inference time",
            "post_hoc_decision": True, "uses_old_false_merge_count": False,
            "per_item_model_mixing": False, "changes_import_rules": False,
            "does_not_authorize_unresolved_drug_import": True,
        },
        "arms": arms,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = rescore(args.root)
    output = args.output or args.root / "comparison-business-v2.json"
    freeze(output, result)
    print(json.dumps({"output": str(output), "selection": result["selection"],
                      "scores": {name: {"disease_direct": a["disease"]["direct_main_correct"],
                                        "disease_total": a["disease"]["items"],
                                        "script_recovered": a["disease"]["script_recovered"],
                                        "drug_identity": a["drugs"]["identity_correct"]}
                                 for name, a in result["arms"].items()}}, ensure_ascii=False))
