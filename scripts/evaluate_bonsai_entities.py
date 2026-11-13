"""Compare Bonsai entities with frozen 010 outputs under the unchanged 016 gate."""
# ruff: noqa: E402

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.source_entity_types import identity, refine
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.evaluate_source_refinement import entity_record
from scripts.evaluate_summary_ablation import arm_timing, verify_quotes
from scripts.evaluate_summary_assist import check_entity_review
from scripts.prepare_bonsai_entities import BASE, IDS, ROOT, RULES

VERSION = "bonsai-entities-evaluation-v1"
ARMS = ("qwen9b", "bonsai27b")


def validate_inputs(root):
    plan = read(root / "plan.json")
    for source in plan["sources"]:
        require(sha(Path(source["path"])) == source["sha256"], "Frozen source changed")
        if source.get("copy"):
            require(sha(root / source["copy"]) == source["sha256"], "Frozen source copy changed")
    require(sha(root / "reference.json") == plan["reference_sha256"], "Entity reference changed")
    dataset, definition = read(root / "inputs.json"), read(root / "definition.json")
    inputs = indexed(dataset["records"], "inputs")
    require(list(inputs) == IDS, "Need the same 15 ordered inputs")
    for row in inputs.values():
        require(row["input_sha256"] == digest({k: v for k, v in row.items() if k != "input_sha256"}), "Input fingerprint differs")
    manifests = {arm: read(folder / "extract/manifest.json") for arm, folder in zip(ARMS, (BASE, root), strict=True)}
    for manifest in manifests.values():
        config = manifest["config"]
        require(manifest["records"] == 15 and manifest["input_sha256"] == digest(dataset)
                and manifest["definition_sha256"] == digest(definition)
                and manifest["config_sha256"] == digest(config)
                and config["definition"] == definition, "Model input/config identity differs")
        require(all(config.get(k) == v for k, v in {
            "max_input_tokens": 4096, "max_new_tokens": 2048, "batch_size": 1,
            "do_sample": False, "enable_thinking": False,
        }.items()), "Unplanned generation change")
    return inputs, manifests


def checked_result(path, input_row, manifest):
    raw = read(path)
    require(raw["id"] == input_row["id"] and raw["input_sha256"] == input_row["input_sha256"]
            and raw["config_sha256"] == manifest["config_sha256"], "Result identity differs")
    if raw["status"] == "classified":
        require(parse(raw["raw_output"]) == raw["payload"], "Raw payload changed")
    else:
        require(raw["payload"] is None, "Incomplete output cannot be rescued")
    return raw


def check_refined_review(review, typed, packet, context):
    same_article = [p for p in context if identity(p) == identity(packet["article"])]
    verify_quotes(review, {"passages": same_article})
    replay = refine(packet, typed["original_entities"], same_article)
    require({k: v for k, v in typed.items() if k != "script_seconds"} == replay,
            "Source type rules do not reproduce")
    checks = review.get("type_rule_checks")
    require(isinstance(checks, list), "Missing type rule review")
    fields = ("entity_id", "from_type", "to_type", "rule_id")
    require(sorted(tuple(c[k] for k in fields) for c in checks)
            == sorted(tuple(c[k] for k in fields) for c in typed["changes"]),
            "Changed types not completely reviewed")
    for check in checks:
        require(type(check.get("supported")) is bool and bool(check.get("brief"))
                and bool(check.get("English_source_evidence")), "Rule review needs source evidence")
    require(type(review.get("refined_core_usable")) is bool
            and isinstance(review.get("refined_core_errors"), list), "Missing refined core decision")
    for error in review["refined_core_errors"]:
        require(error.get("kind") and error.get("problem"), "Unexplained refined core error")
    if not typed["changes"]:
        require(review["refined_core_usable"] == review["core_usable"],
                "Rules cannot change score without changing any type")
    return same_article


def summarize(rows, arm):
    values = [r["arms"][arm] for r in rows]
    reviews = [v["review"] for v in values]
    assembled = [v["assembled"] for v in values]
    entities = [e for a in assembled for e in a["entities"]]
    types = [e for review in reviews for e in review["type_errors"]]
    boundaries = sum(e.get("certainty") == "family_granularity_boundary" for e in types)
    return {
        "groups": len(values),
        **{key: sum(v[key] for v in values) for key in (
            "raw_core_usable", "projected_core_usable", "refined_core_usable", "current_identity_complete")},
        "valid_json": sum(a["raw_json_valid"] for a in assembled),
        "valid_schema": sum(a["schema_valid"] for a in assembled),
        "current_required_items": sum(len(v["current_required_keys"]) for v in values),
        "current_missing_items": sum(len(v["current_missing_keys"]) for v in values),
        "raw_entity_mentions": sum(a["raw_entity_count"] for a in assembled),
        "deduplicated_mentions": len(entities),
        "exact_source_mentions": sum(bool(e["locations"]) for e in entities),
        "nonexact_source_mentions": sum(not e["locations"] for e in entities),
        "type_issues": len(types), "clear_type_issues": len(types) - boundaries,
        "family_granularity_boundaries": boundaries,
        "identity_errors": sum(len(r["identity_errors"]) for r in reviews),
        "extra_issue_groups": sum(bool(r["extra_issues"]) for r in reviews),
        "extra_issue_kinds": dict(Counter(e.get("kind", "unspecified") for r in reviews for e in r["extra_issues"])),
        "type_rule_changes": sum(len(v["type_changes"]) for v in values),
        "standard_link_accuracy": "not_measured",
    }


def paired(rows, field):
    cells = {name: [] for name in ("both_pass", "qwen9b_only", "bonsai27b_only", "both_fail")}
    for row in rows:
        left, right = (row["arms"][arm][field] for arm in ARMS)
        name = "both_pass" if left and right else "qwen9b_only" if left else "bonsai27b_only" if right else "both_fail"
        cells[name].append(row["id"])
    return {k: {"count": len(ids), "ids": ids} for k, ids in cells.items()}


def build(root=ROOT):
    root = Path(root)
    inputs, manifests = validate_inputs(root)
    reference = read(root / "reference.json")
    review = read(root / "review.json")
    require(review["model"] == "gpt-6-astra" and review["status"] == "complete"
            and review["reference_sha256"] == sha(root / "reference.json"), "Need reference-bound completed Astra review")
    packets = indexed(read(root / "source-inputs.json")["records"], "source inputs")
    refs = indexed(reference["records"], "reference")
    legacy = indexed(read(root / "legacy-reference.json")["records"], "legacy reference")
    judgments = indexed(review["records"], "Bonsai review")
    require(set(judgments) == set(packets) == set(refs), "Incomplete entity reference/review")
    context = read(root / "fulltext-context.json")["records"]
    old_assembled = indexed(read(BASE / "assembled.json")["records"], "baseline assembled")
    new_assembled = indexed(read(root / "assembled.json")["records"], "Bonsai assembled")
    old_types = indexed(read(RULES / "entity-types.json")["records"], "baseline source types")
    new_types = indexed(read(root / "entity-types.json")["records"], "Bonsai source types")
    rows, raw_by_arm, sources = [], {arm: [] for arm in ARMS}, []
    for rid, packet in packets.items():
        old_path, new_path = [folder / "extract" / f"result-{rid}.json" for folder in (BASE, root)]
        old_raw = checked_result(old_path, inputs[rid], manifests["qwen9b"])
        new_raw = checked_result(new_path, inputs[rid], manifests["bonsai27b"])
        entity_ref = refs[rid]["entities"]
        old_review = entity_ref["baseline"]
        old = check_entity_review(old_review, old_raw, old_assembled[rid], legacy[rid], entity_ref, packet)
        old_rule = entity_record(RULES, packet, old_assembled[rid], old_types[rid], entity_ref,
                                 refs[rid]["baseline_after_source_rules"], packet, context)
        judgment = judgments[rid]
        require(judgment["result_sha256"] == sha(new_path), "Bonsai reviewed output changed")
        # Raw and class-projection decisions may cite only the original input.
        # The subsequent source-type rules additionally use same-article context.
        core_review = {k: v for k, v in judgment.items()
                       if k not in {"type_rule_checks", "refined_core_usable", "refined_core_errors"}}
        new = check_entity_review(core_review, new_raw, new_assembled[rid], legacy[rid], entity_ref, packet)
        new["review"] = judgment
        check_refined_review(judgment, new_types[rid], packet, context)
        require(judgment["refined_core_usable"] == (
            not judgment["parse_failure"] and not new["current_missing_keys"] and not judgment["refined_core_errors"]),
            "Refined core score differs from reviewed errors/identity")
        arms = {}
        for arm, value, raw, path, typed, refined in (
            ("qwen9b", old, old_raw, old_path, old_types[rid], old_rule["refined"]["core_usable"]),
            ("bonsai27b", new, new_raw, new_path, new_types[rid], judgment["refined_core_usable"]),
        ):
            arms[arm] = {
                **value, "projected_core_usable": value["core_usable"], "refined_core_usable": refined,
                "result_sha256": sha(path), "type_changes": typed["changes"],
                "derived_entities": typed["entities"],
            }
            raw_by_arm[arm].append(raw)
            sources.append(path)
        rows.append({"id": rid, "article": packet["article"], "original_passages": packet["passages"], "arms": arms})
    timing = {}
    for arm, folder in zip(ARMS, (BASE, root), strict=True):
        summary = read(folder / "extract/summary.json")
        timing[arm] = arm_timing(summary, raw_by_arm[arm], manifests[arm])
        timing[arm].update(model_id=manifests[arm]["config"].get("model_id"), precision=manifests[arm]["config"].get("dtype"))
        sources.extend(folder / name for name in ("extract/manifest.json", "extract/summary.json"))
    sources.extend(root / name for name in ("inputs.json", "definition.json", "source-inputs.json", "plan.json",
        "reference.json", "legacy-reference.json", "review.json", "assembled.json", "entity-types.json", "common-projections.json"))
    sources += [Path(__file__), PROJECT / "scripts/prepare_bonsai_entities.py"]
    return {
        "version": VERSION, "stage": "entities", "denominator": 15,
        "scores": {arm: summarize(rows, arm) for arm in ARMS},
        "paired": {key: paired(rows, key) for key in ("raw_core_usable", "projected_core_usable", "refined_core_usable", "current_identity_complete")},
        "timing": {"arms": timing, "new_task_model_calls": 15, "reused_qwen_task_model_calls": 15},
        "records": rows,
        "limits": ["15 reused development groups from five articles; AI reference, not human gold.",
                   "Raw model gate, common class projection, and common source-type rules are separate metrics.",
                   "Reference current core sets are frozen from 015/016; historical 010 identity-only 14/15 is not this gate.",
                   "Standard ID disambiguation and link correctness not evaluated; extraction does not prove relation participation.",
                   "Different runtime and quantization; a deployment comparison, not parameter-count causality."],
        "sources": [{"path": str(path), "sha256": sha(path)} for path in dict.fromkeys(sources)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = build(args.root)
    freeze(args.root / "evaluation.json", result)
    print(json.dumps({"stage": "entities", "scores": result["scores"], "paired": result["paired"]}, ensure_ascii=False, indent=2))
