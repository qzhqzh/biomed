"""Score Bonsai against frozen screening references and existing Qwen outputs."""
# ruff: noqa: E402

import argparse
import json
import math
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.model_comparison import reference_counts, score_predictions
from apps.curation.three_class import (
    load_completed as classification_results,
    load_inputs as classification_inputs,
    validate_output as classification_output,
)
from apps.curation.three_class_evaluation import validate_reference as classification_reference
from scripts.benchmark_knowledge_candidates import completed_results, load_inputs
from scripts.benchmark_passage_screening import (
    completed_results as passage_results,
    load_inputs as passage_inputs,
    validate_output as passage_output,
)
from scripts.evaluate_passage_screening import metrics as passage_metrics
from scripts.prepare_bonsai_screening import (
    BASELINES, CLASS_BASE, CLASS_RUNS, PASSAGE_BASE, VERSION,
    checked_passage_reference, freeze, mapped_inputs, read, sha,
)

ARMS = (*BASELINES, "bonsai27b")
LABELS = {"qwen35-4b": "Qwen3.5-4B", "qwen35-9b": "Qwen3.5-9B", "bonsai27b": "Bonsai 2 27B (PQ2_0)"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def interpreted(raw, stage):
    """Use the old task parser; never rescue a truncated/invalid generic output."""
    key, validator = ("category", classification_output) if stage == "classification" else ("relevant", passage_output)
    result = {key: None, "status": raw["status"], "raw_status": raw["status"],
              "raw_output": raw["raw_output"], "seconds": raw["seconds"],
              "input_tokens": raw["input_tokens"], "output_tokens": raw["output_tokens"]}
    if raw["status"] != "classified":
        return result
    try:
        value = validator(raw["raw_output"])
    except (ValueError, TypeError) as exc:
        result.update(status="invalid_output", task_validation_error=str(exc))
        return result
    result.update({key: value, "status": "abstained" if value is None else "classified"})
    return result


def timing(results, summary, manifest):
    seconds = [r["seconds"] for r in results.values()]
    require(all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in seconds), "Invalid timing")
    total = sum(seconds)
    recorded = summary.get("total_inference_seconds", summary.get("inference_seconds"))
    require(recorded is not None and abs(total - recorded) <= 0.0015, "Timing total differs from raw rows")
    require(summary["state"] == "completed" and summary["processed"] == len(results)
            and summary.get("training") is False, "Incomplete or trained run")
    config = manifest["config"]
    peak = summary.get("nvml_process_peak_mib")
    require(type(peak) in (int, float) and math.isfinite(peak) and peak > 0, "Missing measured GPU memory")
    return {"inference_seconds": total, "mean_seconds": total / len(results),
            "peak_process_mib": peak, "load_seconds": summary.get("load_seconds"),
            "warmup_seconds": summary.get("warmup_seconds"),
            "input_tokens": sum(r.get("input_tokens", 0) for r in results.values()),
            "output_tokens": sum(r.get("output_tokens", 0) for r in results.values()),
            "hardware": summary.get("hardware"), "precision": config.get("dtype"),
            "model_id": config["model_id"], "revision": config["revision"],
            "runtime": config.get("runtime", summary.get("runtime")),
            "timing_scope": summary.get("timing_scope"),
            "measurement_repetitions": summary.get("measurement_repetitions", 1),
            "timing_comparable_full_pass": summary.get("timing_comparable_full_pass", True)}


def paired(rows, refs, predictions, before, stage):
    identity_key = "pmid" if stage == "classification" else "id"
    field = "category" if stage == "classification" else "relevant"
    groups = {k: [] for k in ("both_correct", "bonsai_only_correct", "baseline_only_correct", "both_incorrect", "unscored")}
    changes = {k: [] for k in ("recovered_positive", "newly_missed_positive", "removed_false_positive", "new_false_positive")}
    for row in rows:
        identity = row[identity_key]
        actual = refs[identity][field]
        left, right = predictions[before][identity][field], predictions["bonsai27b"][identity][field]
        if actual is None:
            groups["unscored"].append(identity)
            continue
        left_ok, right_ok = left == actual, right == actual
        key = "both_correct" if left_ok and right_ok else "bonsai_only_correct" if right_ok else "baseline_only_correct" if left_ok else "both_incorrect"
        groups[key].append(identity)
        if stage == "passages":
            if actual is True and left is not True and right is True:
                changes["recovered_positive"].append(identity)
            if actual is True and left is True and right is not True:
                changes["newly_missed_positive"].append(identity)
            if actual is False and left is True and right is False:
                changes["removed_false_positive"].append(identity)
            if actual is False and left is not True and right is True:
                changes["new_false_positive"].append(identity)
    result = {"before": before, "after": "bonsai27b",
              **{k: {"count": len(ids), "ids": ids} for k, ids in groups.items()}}
    if stage == "passages":
        result["selection_changes"] = {k: {"count": len(ids), "ids": ids} for k, ids in changes.items()}
    return result


def validate_frozen(root):
    plan = read(root / "plan.json")
    stage = plan["stage"]
    require(plan["version"] == VERSION and stage in ("classification", "passages"), "Unknown planned experiment")
    for source in plan["sources"]:
        require(sha(source["path"]) == source["sha256"], f"Frozen baseline/source changed: {source['path']}")
    expected_base = CLASS_BASE if stage == "classification" else PASSAGE_BASE
    expected_runs = CLASS_RUNS if stage == "classification" else PASSAGE_BASE
    require(Path(plan["source_base"]) == expected_base
            and plan["baselines"] == {name: str(expected_runs / name) for name in BASELINES}, "Changed baseline selection")
    require((root / "source-inputs.json").read_bytes() == (expected_base / "inputs.json").read_bytes(), "Source inputs changed")
    require(sha(root / "reference.json") == plan["frozen_reference_sha256"] == sha(plan["source_reference"]), "Frozen reference changed")
    source_definition = read(root / "source-definition.json")
    definition = read(root / "definition.json")
    require(definition["system_prompt"] == source_definition["prompt"], "Changed system prompt")
    if stage == "classification":
        require((root / "source-definition.json").read_bytes() == (expected_base / "task-definition-v1.json").read_bytes(), "Original definition changed")
        rows = classification_inputs([root / "source-inputs.json"])
        refs = classification_reference(read(root / "reference.json"), rows, source_definition, sha(root / "source-definition.json"))
    else:
        require((root / "source-definition.json").read_bytes() == (expected_base / "definition.json").read_bytes(), "Original definition changed")
        rows = passage_inputs(root / "source-inputs.json", 282)["records"]
        refs = checked_passage_reference(root, rows, root / "source-definition.json", root / "reference.json")
    inputs = load_inputs(root / "inputs.json")
    require(inputs == mapped_inputs(stage, rows), "Serialized model input differs from original runner")
    require(len(rows) == plan["sample_count"] == (100 if stage == "classification" else 282), "Unexpected sample count")
    return stage, plan, rows, refs, inputs, source_definition, definition


def build(root):
    root = Path(root)
    stage, plan, rows, refs, inputs, source_definition, definition = validate_frozen(root)
    predictions, methods, manifests, paths = {}, {}, {}, []
    for arm in ARMS:
        run = root / "extract" if arm == "bonsai27b" else Path(plan["baselines"][arm])
        manifest, summary = read(run / "manifest.json"), read(run / "summary.json")
        config = manifest["config"]
        require(manifest["config_sha256"] == digest(config), "Model config fingerprint differs")
        for key in ("max_input_tokens", "max_new_tokens", "batch_size", "enable_thinking", "do_sample", "input_truncation"):
            require(config.get(key) == plan["generation"][key], f"Unplanned {key}: {arm}")
        require(manifest["records"] == len(rows), "Manifest record count differs")
        if arm == "bonsai27b":
            require(config["model_id"] == "prism-ml/Ternary-Bonsai-2-27B-gguf", "Not the planned Bonsai model")
            require(config["definition"] == definition and manifest["input_sha256"] == digest(inputs)
                    and manifest["definition_sha256"] == digest(definition), "Bonsai model input identity differs")
            require(read(run / "inputs.json") == inputs, "Bonsai input snapshot differs")
            raw = completed_results(run, inputs["records"], manifest["config_sha256"])
            require(len(raw) == len(rows), "Incomplete Bonsai outputs")
            for result in raw.values():
                require(result.get("server_timings", {}).get("cache_n", 0) == 0, "Unexpected prompt-cache reuse")
            results = {key: interpreted(value, stage) for key, value in raw.items()}
        elif stage == "classification":
            require(config["definition"] == source_definition and manifest["input_sha256"] == digest(rows), "Classification baseline input differs")
            require(classification_inputs([run / "inputs.json"]) == rows, "Classification baseline snapshot differs")
            raw = results = classification_results(run, rows, manifest["config_sha256"])
        else:
            require(config["definition"] == source_definition
                    and manifest["input_sha256"] == digest(read(root / "source-inputs.json")), "Passage baseline input differs")
            require(read(run / "inputs.json") == read(root / "source-inputs.json"), "Passage baseline snapshot differs")
            raw = results = passage_results(run, rows, manifest["config_sha256"])
        require(len(results) == len(rows), "Incomplete outputs")
        resources = timing(raw, summary, manifest)
        if stage == "classification":
            score = score_predictions(rows, refs, results)
        else:
            score = {**passage_metrics(rows, refs, results),
                     "by_paper": {pmid: passage_metrics([r for r in rows if r["pmid"] == pmid], refs, results)
                                  for pmid in dict.fromkeys(r["pmid"] for r in rows)}}
        methods[arm] = {"id": arm, "label": LABELS[arm], **score, "timing": resources}
        predictions[arm] = results
        manifests[arm] = manifest
        paths += [run / "manifest.json", run / "summary.json"]
        paths += sorted(run.glob("result-*.json"))
    identity_key = "pmid" if stage == "classification" else "id"
    cases = [{"id": row[identity_key], "source": row, "reference": refs[row[identity_key]],
              "predictions": {arm: predictions[arm][row[identity_key]] for arm in ARMS}} for row in rows]
    counts = reference_counts(rows, refs) if stage == "classification" else {
        "sample_count": len(rows), "denominator": sum(r["relevant"] is not None for r in refs.values()),
        "positive": sum(r["relevant"] is True for r in refs.values()), "negative": sum(r["relevant"] is False for r in refs.values()),
        "unscored": sum(r["relevant"] is None for r in refs.values())}
    paths += [root / name for name in ("plan.json", "inputs.json", "definition.json", "source-inputs.json", "source-definition.json", "reference.json")]
    paths += [Path(__file__), PROJECT / "scripts/prepare_bonsai_screening.py"]
    return {"version": VERSION, "stage": stage, **counts,
            "reference_sha256": sha(root / "reference.json"), "methods": methods,
            "paired": {arm: paired(rows, refs, predictions, arm, stage) for arm in BASELINES}, "cases": cases,
            "generation": plan["generation"],
            "model_configs": {arm: manifest["config"] for arm, manifest in manifests.items()},
            "new_baseline_model_calls": 0, "new_bonsai_scored_calls": len(rows),
            "limits": ["Frozen reused development data, not an independent holdout or clinical validation.",
                       "Unchanged Astra AI reference, not human gold; unresolved reference labels are excluded from accuracy denominators.",
                       "Unresolved/invalid outputs are never converted into non-neoplastic papers or irrelevant paragraphs.",
                       "Single measured pass per deployment; quantization, native templates and runtime differ, so gains are not attributable only to parameter count.",
                       "No training, production knowledge import or tumor_api changes."],
            "sources": [{"path": str(path), "sha256": sha(path)} for path in dict.fromkeys(paths)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="One stage directory")
    args = parser.parse_args()
    evaluation = build(args.root)
    freeze(args.root / "evaluation.json", evaluation)
    print(json.dumps({arm: {key: method.get(key) for key in
          (("matched", "denominator", "macro_recall", "macro_f1") if evaluation["stage"] == "classification"
           else ("tp", "fp", "fn", "precision", "recall", "f1"))} for arm, method in evaluation["methods"].items()}, ensure_ascii=False))
