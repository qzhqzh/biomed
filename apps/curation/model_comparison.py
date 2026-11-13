"""Evaluate offline benchmark runs against the unchanged, source-checked reference."""

import math
import statistics

from apps.curation.decision import digest
from apps.curation.three_class import CATEGORIES, load_completed, load_inputs
from apps.curation.three_class_evaluation import file_hash, method_summary, outcome, read_json, validate_reference


def score_predictions(rows, references, predictions):
    if set(predictions) != {row["pmid"] for row in rows}:
        raise ValueError("Incomplete or unexpected predictions cannot be scored")
    cases = []
    for row in rows:
        ref, result = references[row["pmid"]], predictions[row["pmid"]]
        cases.append({"reference_relevance": ref["category"], "model_scope": result["category"],
            "model_status": result["status"],
            "model_outcome": outcome(result["category"], ref["category"], result["status"])})
    summary = method_summary(cases, "model")
    by_class = summary["by_class"]
    for category in CATEGORIES:
        tp = by_class[category]["matched"]
        fp = sum(c["reference_relevance"] is not None and c["reference_relevance"] != category
                 and c["model_scope"] == category for c in cases)
        fn = by_class[category]["denominator"] - tp
        by_class[category].update(precision=tp / (tp + fp) if tp + fp else 0,
            recall=tp / (tp + fn) if tp + fn else 0,
            f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    summary["macro_f1"] = sum(c["f1"] for c in by_class.values()) / len(CATEGORIES)
    summary["tumor_false_exclusions"] = sum(c["reference_relevance"] in ("solid", "hematologic")
        and c["model_scope"] == "non_neoplastic" for c in cases)
    return summary


def pairwise_comparisons(methods, cases, *, include_resources=True):
    """Describe observed changes; do not attribute cross-family gains to size alone."""
    pairs = [
        ("Qwen3-1.7B", "GLiNER2.5-Decide", "更换模型与架构"),
        ("Qwen3-1.7B", "Qwen3.5-4B", "更换代际并增大规模"),
        ("Qwen3-1.7B", "Qwen3-8B", "Qwen3 同系列增大规模"),
        ("Qwen3.5-4B", "Qwen3.5-9B", "Qwen3.5 同系列增大规模"),
    ]
    available = {method["label"]: method for method in methods}
    result = []
    for before, after, question in pairs:
        if before not in available or after not in available:
            continue
        left, right = available[before], available[after]
        if left["denominator"] != right["denominator"]:
            raise ValueError("Pairwise denominator mismatch")
        improved = regressed = 0
        for case in cases:
            if case["reference"] is None:
                continue
            predictions = {p["label"]: p for p in case["predictions"]}
            was_right = predictions[before]["outcome"] == "matched"
            now_right = predictions[after]["outcome"] == "matched"
            improved += not was_right and now_right
            regressed += was_right and not now_right
        comparison = {"question": question, "before": before, "after": after,
            "denominator": left["denominator"],
            "accuracy_delta_pp": (right["matched"] - left["matched"]) / left["denominator"] * 100 if left["denominator"] else None,
            "macro_recall_delta_pp": (right["macro_recall"] - left["macro_recall"]) * 100
                if left["macro_recall"] is not None and right["macro_recall"] is not None else None,
            "correct_delta": right["matched"] - left["matched"], "improved": improved, "regressed": regressed}
        if include_resources:
            comparison.update(parameter_ratio=right["parameters"] / left["parameters"],
                time_ratio=right["mean_seconds"] / left["mean_seconds"],
                memory_ratio=right["nvml_process_peak_mib"] / left["nvml_process_peak_mib"])
        result.append(comparison)
    return result


def split_original_and_added(rows, references, definition, original_base):
    """Verify the original cohort is unchanged before reporting the new cohort."""
    original_rows = load_inputs([original_base / "inputs.json"])
    original_definition = read_json(original_base / "task-definition-v1.json")
    if original_definition != definition:
        raise ValueError("Original task definition changed")
    original_references = validate_reference(read_json(original_base / "astra-reference-v1.json"),
        original_rows, original_definition, file_hash(original_base / "task-definition-v1.json"))
    current = {row["pmid"]: row for row in rows}
    for row in original_rows:
        pmid = row["pmid"]
        if current.get(pmid) != row:
            raise ValueError("Original cohort input changed or is missing")
        if references[pmid] != original_references[pmid]:
            raise ValueError("Original cohort reference changed")
    original_ids = {row["pmid"] for row in original_rows}
    added = [row for row in rows if row["pmid"] not in original_ids]
    if not added:
        raise ValueError("Expanded cohort must contain new papers")
    return {"original": original_rows, "added": added}


def reference_counts(rows, references):
    by_class = {category: sum(references[row["pmid"]]["category"] == category for row in rows)
        for category in CATEGORIES}
    denominator = sum(by_class.values())
    return {"sample_count": len(rows), "denominator": denominator,
        "unscored": len(rows) - denominator, "reference_by_class": by_class}


def evaluate_runs(base, runs, *, sample_count=None, original_base=None):
    rows = load_inputs([base / "inputs.json"])
    if sample_count is not None and len(rows) != sample_count:
        raise ValueError(f"Expected {sample_count} frozen papers, got {len(rows)}")
    definition = read_json(base / "task-definition-v1.json")
    reference = read_json(base / "astra-reference-v1.json")
    references = validate_reference(reference, rows, definition, file_hash(base / "task-definition-v1.json"))
    cohorts = split_original_and_added(rows, references, definition, original_base) if original_base else {}
    methods, predictions = [], {}
    ids = set()
    for run in runs:
        manifest = read_json(run / "manifest.json")
        config = manifest["config"]
        summary = read_json(run / "summary.json")
        if (manifest["config_sha256"] != digest(config) or manifest["input_sha256"] != digest(rows)
            or config["definition"] != definition or summary.get("state") != "completed"
            or summary.get("processed") != len(rows) or summary.get("training") is not False):
            raise ValueError("Run does not match frozen task/input, or is not complete and untrained")
        if load_inputs([run / "inputs.json"]) != rows:
            raise ValueError("Run input snapshot differs")
        results = load_completed(run, rows, manifest["config_sha256"])
        model_id = config["model_id"]
        if model_id in ids:
            raise ValueError("Select exactly one predeclared run per model")
        ids.add(model_id)
        scored = score_predictions(rows, references, results)
        seconds = [result["seconds"] for result in results.values()]
        if any(not math.isfinite(value) or value <= 0 for value in seconds):
            raise ValueError("Invalid timing measurement")
        method = {"id": run.name, "label": model_id.split("/")[-1], "model_id": model_id,
            "parameters": summary["parameter_count"], "precision": config["dtype"],
            "revision": config["revision"], "config_sha256": manifest["config_sha256"],
            "mean_seconds": sum(seconds) / len(seconds), "median_seconds": statistics.median(seconds),
            "p95_seconds": sorted(seconds)[math.ceil(0.95 * len(seconds)) - 1],
            "percentile_method": "nearest_rank_from_raw_results",
            "timing": summary, **scored,
            **{key: summary[key] for key in ("peak_allocated_mib", "peak_reserved_mib", "nvml_process_peak_mib")}}
        methods.append(method)
        predictions[run.name] = results
    cases = []
    for row in rows:
        ref = references[row["pmid"]]
        cases.append({"pmid": row["pmid"], "title": row["metadata"]["title"],
            "reference": ref["category"], "reference_notes": ref["reason"], "reference_quotes": ref["evidence"],
            "predictions": [{"method_id": method["id"], "label": method["label"],
                **{key: predictions[method["id"]][row["pmid"]][key] for key in ("category", "status", "seconds", "raw_output")},
                "outcome": outcome(predictions[method["id"]][row["pmid"]]["category"], ref["category"],
                    predictions[method["id"]][row["pmid"]]["status"])} for method in methods]})
    groups = {}
    for group_id, group_rows in cohorts.items():
        pmids = {row["pmid"] for row in group_rows}
        group_methods = [{"id": method["id"], "label": method["label"],
            **score_predictions(group_rows, references,
                {pmid: result for pmid, result in predictions[method["id"]].items() if pmid in pmids})}
            for method in methods]
        group_cases = [case for case in cases if case["pmid"] in pmids]
        for case in group_cases:
            case["group"] = group_id
        groups[group_id] = {"label": f"{'原' if group_id == 'original' else '新增'}{len(group_rows)}篇",
            **reference_counts(group_rows, references), "pmids": [row["pmid"] for row in group_rows],
            "methods": group_methods,
            "pairwise": pairwise_comparisons(group_methods, group_cases, include_resources=False)}
    return {**reference_counts(rows, references), "input_sha256": digest(rows), "definition": definition,
        "definition_sha256": file_hash(base / "task-definition-v1.json"),
        "reference_sha256": file_hash(base / "astra-reference-v1.json"), "methods": methods, "cases": cases,
        "pairwise": pairwise_comparisons(methods, cases), "groups": groups}
