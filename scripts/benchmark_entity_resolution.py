"""Run/evaluate the frozen local 9B/Bonsai entity disambiguation comparison."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.light_entity_linking import _key
from scripts.prepare_entity_resolution import DEFAULT, freeze, read

PYTHON = Path("/appdata/02_resource/apps/biomed/research/runtime/bin/python")
BONSAI = Path("/appdata/02_resource/apps/biomed/research/runtime-bonsai")
VERSION = "entity-resolution-comparison-v1"
FIELDS = {"decision", "candidate_id", "match_relation", "main_cancer_id",
          "subtype_candidate_id", "qualifiers", "evidence_quote", "reason"}
IDENTITY_FIELDS = ("decision", "candidate_id", "match_relation")
CLASSIFICATION_FIELDS = ("main_cancer_id", "subtype_candidate_id")


def check_reference(root, reference):
    inputs = read(root / "qwen35-9b/inputs.json")
    if read(root / "bonsai2-27b/inputs.json") != inputs:
        raise ValueError("The arms no longer have identical task inputs")
    if reference.get("inputs_sha256") != digest(inputs):
        raise ValueError("Reference is not bound to the frozen inputs")
    items = read(root / "items.json")["records"]
    rows = reference.get("records", [])
    if [r["id"] for r in rows] != [r["id"] for r in items]:
        raise ValueError("Reference must cover every frozen item in order")
    for item, row in zip(items, rows, strict=True):
        if not row.get("accepted"):
            raise ValueError(f"Missing accepted reference for {item['id']}")
        candidates = {c["id"] for c in item["candidates"]}
        main = {c["id"] for c in item["main_cancer_candidates"]}
        for answer in row["accepted"]:
            if set(answer) != set(IDENTITY_FIELDS + CLASSIFICATION_FIELDS):
                raise ValueError("Accepted references require five decision fields")
            if answer["candidate_id"] is not None and answer["candidate_id"] not in candidates:
                raise ValueError("Reference selects unavailable identity candidate")
            if answer["main_cancer_id"] is not None and answer["main_cancer_id"] not in main:
                raise ValueError("Reference selects unavailable main cancer")
            if answer["subtype_candidate_id"] is not None and answer["subtype_candidate_id"] not in candidates:
                raise ValueError("Reference selects unavailable subtype")
    return inputs, items


def check_prediction(item, value):
    errors = []
    if not isinstance(value, dict) or set(value) != FIELDS:
        return ["wrong_output_schema"]
    decision = value["decision"]
    if not isinstance(decision, str) or not isinstance(value["match_relation"], str):
        return ["invalid_decision_type"]
    if any(value[k] is not None and not isinstance(value[k], str)
           for k in ("candidate_id", "main_cancer_id", "subtype_candidate_id")):
        return ["invalid_candidate_type"]
    if decision not in {"MATCH", "NEW", "UNRESOLVED", "NOT_ENTITY"}:
        errors.append("invalid_decision")
    candidate_ids = {c["id"] for c in item["candidates"]}
    main_ids = {c["id"] for c in item["main_cancer_candidates"]}
    if decision == "MATCH":
        if value["candidate_id"] not in candidate_ids:
            errors.append("invented_or_missing_candidate")
    elif value["candidate_id"] is not None:
        errors.append("nonmatch_has_candidate")
    if value["main_cancer_id"] is not None and value["main_cancer_id"] not in main_ids:
        errors.append("invented_main_cancer")
    if value["subtype_candidate_id"] is not None and value["subtype_candidate_id"] not in candidate_ids:
        errors.append("invented_subtype")
    if item["kind"] == "drug" and (value["main_cancer_id"] is not None or value["subtype_candidate_id"] is not None):
        errors.append("drug_has_cancer_classification")
    relations = {"MATCH": {"exact_identity", "alias", "qualified_form"}, "NEW": {"new_entity"},
                 "UNRESOLVED": {"unresolved"}, "NOT_ENTITY": {"not_entity"}}
    if value["match_relation"] not in relations.get(decision, set()):
        errors.append("invalid_match_relation")
    texts = [c["quote"] for c in item["contexts"]]
    quote = value["evidence_quote"]
    if not isinstance(quote, str) or not quote or not any(quote in text for text in texts):
        errors.append("quote_not_in_supplied_source")
    qualifiers = value["qualifiers"]
    if not isinstance(qualifiers, list) or any(not isinstance(q, str) or not q for q in qualifiers):
        errors.append("invalid_qualifiers")
    elif any(not any(q in text for text in texts) for q in qualifiers):
        errors.append("qualifier_not_in_supplied_source")
    if not isinstance(value["reason"], str) or not value["reason"]:
        errors.append("missing_reason")
    return errors


def score_row(item, reference, result):
    value = result.get("payload")
    errors = check_prediction(item, value) if result.get("status") == "classified" else [result.get("status", "missing_output")]
    value = value if isinstance(value, dict) else {}
    accepted = reference["accepted"]
    classified = result.get("status") == "classified"
    identity = classified and any(all(k in value and value[k] == answer[k] for k in IDENTITY_FIELDS) for answer in accepted)
    classification = classified and any(all(k in value and value[k] == answer[k] for k in CLASSIFICATION_FIELDS) for answer in accepted)
    joint = classified and any(all(k in value and value[k] == answer[k] for k in IDENTITY_FIELDS + CLASSIFICATION_FIELDS) for answer in accepted)
    qualifiers = value.get("qualifiers", [])
    text = _key(" ".join(q for q in qualifiers if isinstance(q, str))) if isinstance(qualifiers, list) else ""
    missing = [alternatives for alternatives in reference.get("required_qualifiers", [])
               if not any(_key(term) in text for term in alternatives)]
    qualifiers_correct = classified and not missing and not any("qualifier" in e for e in errors)
    correct_target = any(a["decision"] == "MATCH" and a["candidate_id"] == value.get("candidate_id") for a in accepted)
    return {"id": item["id"], "cohort": item["cohort"], "label": item["label"], "kind": item["kind"],
            "protocol_valid": not errors, "identity_correct": identity,
            "classification_correct": classification, "qualifiers_correct": qualifiers_correct,
            "all_correct": not errors and joint and qualifiers_correct,
            "false_merge": value.get("decision") == "MATCH" and not correct_target,
            "missed_existing_identity": value.get("decision") in ("NEW", "UNRESOLVED")
            and all(a["decision"] == "MATCH" for a in accepted),
            "unresolved": value.get("decision") == "UNRESOLVED",
            "protocol_errors": errors, "missing_qualifiers": missing, "prediction": value,
            "accepted": accepted, "reference_reason": reference.get("reason", ""),
            "seconds": result.get("seconds"), "raw_status": result.get("status", "missing_output")}


def aggregate(rows):
    keys = ["protocol_valid", "identity_correct", "classification_correct", "qualifiers_correct",
            "all_correct", "false_merge", "missed_existing_identity", "unresolved"]
    return {"items": len(rows), **{k: sum(bool(r[k]) for r in rows) for k in keys}}


def evaluate(root, reference_path):
    reference = read(reference_path)
    _, items = check_reference(root, reference)
    plan = read(root / "evaluation-plan.json")
    if plan["reference_sha256"] != digest(reference):
        raise ValueError("Reference changed after inference plan")
    arms = {}
    for arm in ["qwen35-9b", "bonsai2-27b"]:
        folder = root / arm / "extract"
        rows = []
        for item, expected in zip(items, reference["records"], strict=True):
            path = folder / f"result-{item['id']}.json"
            rows.append(score_row(item, expected, read(path) if path.exists() else {}))
        summary = read(folder / "summary.json")
        arms[arm] = {"overall": aggregate(rows), "cohorts": {
            cohort: aggregate([r for r in rows if r["cohort"] == cohort])
            for cohort in sorted({r["cohort"] for r in rows})},
            "records": rows, "inference_summary": summary,
            "warm_seconds": round(sum(r["seconds"] or 0 for r in rows), 6)}
    def rank(name):
        score = arms[name]["cohorts"]["unresolved_after_rules"]
        # Identity errors are especially costly; end-to-end field completeness
        # then resolves ties, before runtime. Never a per-item best-of ensemble.
        return (-score["identity_correct"], score["false_merge"], -score["all_correct"],
                -score["classification_correct"], arms[name]["warm_seconds"])
    selected = min(arms, key=rank)
    operational = [r for r in arms[selected]["records"] if r["cohort"] == "unresolved_after_rules"]
    passed = [r for r in operational if r["all_correct"]]
    review = [r for r in operational if not r["all_correct"] or r["prediction"].get("decision") == "UNRESOLVED"]
    selected_items = {i["id"]: i for i in items}
    report = {"version": VERSION, "reference_sha256": digest(reference),
              "selection": {"model": selected, "rule": "identity accuracy, false merges, full result, classification, then warm seconds",
                            "no_per_item_cherry_picking": True, "model_replaces_confirmed_rules": False,
                            "model_outputs_are_proposals_not_human_confirmation": True,
                            "reference_pass_item_ids": [r["id"] for r in passed],
                            "reference_pass_mentions": sum(len(selected_items[r["id"]]["mentions"]) for r in passed),
                            "passed_by_decision": {d: [r["id"] for r in passed if r["prediction"].get("decision") == d]
                                                   for d in ("MATCH", "NEW", "UNRESOLVED", "NOT_ENTITY")},
                            "review_item_ids": [r["id"] for r in review]},
              "arms": arms, "scope": "19 operational unresolved terms plus one separately scored known-risk challenge; development test, no general accuracy claim",
              "target_online_access": False, "model_training": False}
    freeze(root / "comparison.json", report)
    return report


def run(root, reference_path, arm):
    reference = read(reference_path)
    inputs, _ = check_reference(root, reference)
    plan = {"version": VERSION, "reference_sha256": digest(reference), "inputs_sha256": digest(inputs),
            "reference_frozen_before_inference": True, "max_input_tokens": 4096, "max_new_tokens": 512,
            "arms": ["qwen35-9b", "bonsai2-27b"], "target_online_access": False}
    freeze(root / "evaluation-plan.json", plan)
    names = plan["arms"] if arm == "both" else [arm]
    for name in names:
        folder = root / name
        if name == "qwen35-9b":
            command = [str(PYTHON), "scripts/benchmark_knowledge_candidates.py", "--inputs", str(folder / "inputs.json"),
                       "--definition", str(folder / "definition.json"), "--output", str(folder / "extract"),
                       "--max-input-tokens", "4096", "--max-new-tokens", "512", "--resume"]
        else:
            command = [str(PYTHON), "scripts/benchmark_bonsai_stage.py", "--root", str(folder),
                       "--max-input-tokens", "4096", "--max-new-tokens", "512"]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["LD_LIBRARY_PATH"] = str(BONSAI / "cuda-12.4/lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / f"{name}.log").open("a") as log:
            subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        print(json.dumps({"arm": name, "status": "completed"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "evaluate"])
    parser.add_argument("--root", type=Path, default=DEFAULT)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--arm", choices=["both", "qwen35-9b", "bonsai2-27b"], default="both")
    args = parser.parse_args()
    ref = args.reference or args.root / "reference.json"
    if args.command == "run":
        run(args.root, ref, args.arm)
    else:
        result = evaluate(args.root, ref)
        print(json.dumps({"selection": result["selection"], "scores": {k: v["overall"] for k, v in result["arms"].items()}}, ensure_ascii=False))
