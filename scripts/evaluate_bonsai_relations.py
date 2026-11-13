"""Compare a new Bonsai run with frozen 014 outputs; never rerun either model."""

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.evaluate_summary_ablation import (
    arm_timing,
    check_review,
    common_digest,
    summarize,
    verify_quotes,
)
from scripts.summary_ablation_pilot import COMMON_KEYS, compare_common_schema

VERSION = "bonsai-relations-evaluation-v1"
ARMS = ("qwen9b", "bonsai27b")
BASE = PROJECT / "var/experiments/summary-ablation-20261029"
ROOT = PROJECT / "var/experiments/bonsai-relations-20261030"
IDS = [f"K{i:02d}" for i in range(1, 16)]


def mechanical_check(packet, result):
    """Use the unchanged 014 no-statement contract and its fixed diagnostics."""
    return compare_common_schema(packet, result, result)["arms"]["without_summary"]


def checked_arm(packet, raw, review, path, manifest, item):
    require(
        raw["id"] == packet["id"]
        and raw["input_sha256"] == item["input_sha256"]
        and raw["config_sha256"] == manifest["config_sha256"]
        and review["result_sha256"] == sha(path),
        f"Reviewed output identity differs: {packet['id']}",
    )
    if raw["status"] == "classified":
        require(
            parse(raw["raw_output"]) == raw["payload"],
            "Model text differs from payload",
        )
    else:
        require(raw["payload"] is None, "Partial model output must not be rescued")
    payload = raw.get("payload")
    fact = payload.get("fact") if isinstance(payload, dict) else None
    require(
        review["common_fields_sha256"] == common_digest(fact),
        "Review does not describe this raw fact",
    )
    check_review(review, packet)
    if raw["status"] != "classified" or not isinstance(fact, dict):
        require(not review["structured_common_core_usable"], "Missing fact cannot pass")
    mechanical = mechanical_check(packet, raw)
    strict = (
        mechanical["raw"]["status"] == "common_structurally_valid"
        and not mechanical["raw"]["format_changes"]
    )
    diagnostic = mechanical["diagnostic"]["status"] == "common_structurally_valid"
    core = review["structured_common_core_usable"]
    return {
        "result_sha256": sha(path),
        "raw_status": raw["status"],
        "raw_fact": fact,
        "review": review,
        "mechanical": mechanical,
        "strict_schema_valid": strict,
        "diagnostic_schema_valid": diagnostic,
        "strict_core_ready": strict and core,
        "diagnostic_core_ready": diagnostic and core,
        "complete_task_ready": diagnostic
        and core
        and review["axes"]["anchor_focus"] is True,
        "seconds": raw["seconds"],
        "input_tokens": raw["input_tokens"],
        "output_tokens": raw["output_tokens"],
    }


def paired(records, selector):
    cells = {
        k: []
        for k in (
            "both_pass",
            "qwen9b_only",
            "bonsai27b_only",
            "both_fail",
            "not_applicable",
        )
    }
    for row in records:
        left, right = [selector(row["arms"][arm]) for arm in ARMS]
        if left is None or right is None:
            key = "not_applicable"
        elif left and right:
            key = "both_pass"
        elif left:
            key = "qwen9b_only"
        elif right:
            key = "bonsai27b_only"
        else:
            key = "both_fail"
        cells[key].append(row["id"])
    return {key: {"count": len(ids), "ids": ids} for key, ids in cells.items()}


def validate_inputs(root, baseline):
    for name in ("inputs.json", "packets.json", "definition.json"):
        require(
            (root / name).read_bytes() == (baseline / name).read_bytes(),
            f"Frozen 014 {name} changed; do not regenerate the K07 input",
        )
    dataset, definition = read(root / "inputs.json"), read(root / "definition.json")
    inputs = indexed(dataset["records"], "inputs")
    packets = indexed(read(root / "packets.json")["records"], "packets")
    require(
        list(inputs) == list(packets) == IDS, "Need the same ordered 15 frozen groups"
    )
    for item in inputs.values():
        require(
            item["input_sha256"]
            == digest({k: v for k, v in item.items() if k != "input_sha256"}),
            "Input fingerprint differs",
        )
    manifests = {
        arm: read(path / "extract/manifest.json")
        for arm, path in zip(ARMS, (baseline, root), strict=True)
    }
    for arm, manifest in manifests.items():
        require(
            manifest["records"] == 15
            and manifest["config_sha256"] == digest(manifest["config"])
            and manifest["input_sha256"] == digest(dataset)
            and manifest["definition_sha256"] == digest(definition)
            and manifest["config"]["definition"] == definition,
            f"Inference manifest differs: {arm}",
        )
        config = manifest["config"]
        require(
            config.get("max_new_tokens") == 2048
            and config.get("batch_size") == 1
            and config.get("do_sample") is False
            and config.get("enable_thinking") is False,
            f"Unplanned decoding change: {arm}",
        )
    return inputs, packets, manifests


def build(root=ROOT, baseline=BASE):
    root, baseline = Path(root), Path(baseline)
    inputs, packets, manifests = validate_inputs(root, baseline)
    reference, reviews = read(root / "reference.json"), read(root / "review.json")
    require(
        reference["model"] == reviews["model"] == "gpt-6-astra"
        and reviews["status"] == "complete"
        and reviews["reference_sha256"] == sha(root / "reference.json")
        and reviews["policy_version"] == reference["policy_version"],
        "Need completed, reference-bound Astra review of both arms",
    )
    refs, reviewed = (
        indexed(reference["records"], "reference"),
        indexed(reviews["records"], "review"),
    )
    require(
        set(refs) == set(reviewed) == set(packets), "Incomplete paired reference/review"
    )
    records, paths = [], []
    raw_by_arm = {arm: [] for arm in ARMS}
    for rid, packet in packets.items():
        verify_quotes(refs[rid], packet)
        require(
            set(reviewed[rid]["arms"]) == set(ARMS), "Review must include both models"
        )
        arms = {}
        for arm, run_root in zip(ARMS, (baseline, root), strict=True):
            path = run_root / "extract" / f"result-{rid}.json"
            raw = read(path)
            arms[arm] = checked_arm(
                packet,
                raw,
                reviewed[rid]["arms"][arm],
                path,
                manifests[arm],
                inputs[rid],
            )
            raw_by_arm[arm].append(raw)
            paths.append(path)
        error_sets = [
            {e["kind"] for e in arms[arm]["review"]["errors"]} for arm in ARMS
        ]
        blocking = [
            {e["kind"] for e in arms[arm]["review"]["errors"] if e["blocking"]}
            for arm in ARMS
        ]
        records.append(
            {
                "id": rid,
                "article": packet["article"],
                "original_passages": packet["passages"],
                "arms": arms,
                "error_changes": {
                    "introduced_kinds": sorted(error_sets[1] - error_sets[0]),
                    "resolved_kinds": sorted(error_sets[0] - error_sets[1]),
                    "persistent_kinds": sorted(error_sets[0] & error_sets[1]),
                    "introduced_blocking_kinds": sorted(blocking[1] - blocking[0]),
                    "resolved_blocking_kinds": sorted(blocking[0] - blocking[1]),
                },
            }
        )
    timing, scores = {}, {}
    for arm, run_root in zip(ARMS, (baseline, root), strict=True):
        summary = read(run_root / "extract/summary.json")
        timing[arm] = arm_timing(summary, raw_by_arm[arm], manifests[arm])
        timing[arm]["parameter_count"] = summary.get("parameter_count")
        timing[arm]["runtime"] = summary.get("runtime")
        timing[arm]["model_id"] = manifests[arm]["config"].get("model_id")
        timing[arm]["precision"] = manifests[arm]["config"].get("dtype")
        scores[arm] = summarize(records, arm)
        scores[arm]["complete_task_ready"] = sum(
            row["arms"][arm]["complete_task_ready"] for row in records
        )
        paths.extend(
            run_root / name
            for name in ("extract/manifest.json", "extract/summary.json")
        )
    comparisons = {
        "common_core": paired(
            records, lambda row: row["review"]["structured_common_core_usable"]
        ),
        "anchor_focus": paired(
            records, lambda row: row["review"]["axes"]["anchor_focus"]
        ),
        **{
            key: paired(records, lambda row, key=key: row[key])
            for key in (
                "strict_core_ready",
                "diagnostic_core_ready",
                "complete_task_ready",
            )
        },
    }
    paths.extend(
        root / name
        for name in (
            "inputs.json",
            "packets.json",
            "definition.json",
            "reference.json",
            "review.json",
            "plan.json",
        )
    )
    paths.extend(
        baseline / name for name in ("inputs.json", "packets.json", "definition.json")
    )
    paths.extend(
        PROJECT / name
        for name in (
            "scripts/evaluate_bonsai_relations.py",
            "scripts/evaluate_summary_ablation.py",
            "scripts/summary_ablation_pilot.py",
            "scripts/relation_format_diagnostic.py",
            "apps/curation/core_knowledge_name_resolution.py",
            "apps/curation/core_knowledge_optional_entities.py",
        )
    )
    delta = (
        timing["bonsai27b"]["inference_seconds"] - timing["qwen9b"]["inference_seconds"]
    )
    return {
        "version": VERSION,
        "policy_version": reference["policy_version"],
        "denominator": 15,
        "state": "research_comparison_not_knowledge_export",
        "baseline_experiment": "summary-ablation-20261029",
        "scores": scores,
        "paired": comparisons,
        "complete_task_delta_percentage_points": (
            scores["bonsai27b"]["complete_task_ready"]
            - scores["qwen9b"]["complete_task_ready"]
        )
        / 15
        * 100,
        "timing": {
            "arms": timing,
            "delta_seconds_bonsai_minus_qwen": delta,
            "new_task_model_calls": 15,
            "reused_baseline_task_model_calls": 15,
            "new_qwen9b_task_model_calls": 0,
            "new_repair_calls": 0,
            "review_timing_included": False,
        },
        "records": records,
        "validation": {
            "input_packet_and_definition_bytes_identical_to_014": True,
            "baseline_raw_outputs_reused": True,
            "both_arms_new_reference_review": True,
            "common_fields": sorted(COMMON_KEYS),
            "complete_task_definition": "014 diagnostic structure passes AND reviewed common core usable AND ANCHOR focused",
            "diagnostics": "Same 014 format adapter, exact source-name alignment and optional entity-ID deferral for both arms; no semantic repair or new model call",
            "run_configurations": {arm: manifests[arm]["config"] for arm in ARMS},
        },
        "limits": [
            "Five articles and 15 reused development groups; a single run per model, not independent clinical validation or full-library accuracy.",
            "Both old 9B outputs and new Bonsai outputs are reviewed under this experiment's common policy. Old 014/018 scores are not reused as model scores.",
            "Runtime, precision, native chat templates and tokenizers may differ; latency and memory describe these deployments, not a causal parameter-count scaling result. Token totals are not directly comparable across tokenizers.",
            "No training, repair inference, production knowledge import or tumor_api modification; Astra reference is not human gold.",
            "Paired error categories describe this sample only; absence of a category does not prove an identical fact was repaired.",
        ],
        "sources": [
            {"path": str(path), "sha256": sha(path)} for path in dict.fromkeys(paths)
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--baseline", type=Path, default=BASE)
    args = parser.parse_args()
    result = build(args.root, args.baseline)
    freeze(args.root / "evaluation.json", result)
    print(
        json.dumps(
            {"scores": result["scores"], "paired": result["paired"]},
            ensure_ascii=False,
            indent=2,
        )
    )
