#!/usr/bin/env python3
"""Quick, offline comparison of frozen 4B outputs with the existing v3 rules."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.model_comparison import score_predictions
from apps.curation.three_class import load_completed, load_inputs
from apps.curation.three_class_evaluation import (
    IDENTITY_FIELDS, RULE_MAPPING, file_hash, read_json, validate_quote, validate_reference,
)
from scripts.evidence_pilot import setup_helpers

BASE = ROOT / "var/experiments/three-class-100-20261023"
RUN = ROOT / "var/experiments/model-comparison-100-20261023/qwen35-4b"
OUTPUT = ROOT / "var/experiments/four-b-rules-quick-20261023"
TAXONOMY = ROOT / "apps/curation/vocabularies/solid-tumor-v3.json"
DECIDING_LABELS = {
    "solid": {"solid_malignancy", "solid_cell_model"},
    "hematologic": {"hematologic", "hematologic_abbreviation"},
}
METHODS = {
    "A": "Frozen Qwen3.5-4B, unchanged",
    "B": "Existing RULE_MAPPING overrides 4B whenever the mapping is non-null",
    "C": "Override only 4B non_neoplastic when existing rules map to solid/hematologic",
    "D": "B, but a label deciding the mapped scope must have a title or MeSH match",
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--run", type=Path, default=RUN)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    rows = load_inputs([args.base / "inputs.json"])
    if len(rows) != 100 or len({row["pmid"] for row in rows}) != 100:
        raise ValueError("Expected exactly 100 unique frozen papers")
    definition_path = args.base / "task-definition-v1.json"
    definition = read_json(definition_path)
    references = validate_reference(
        read_json(args.base / "astra-reference-v1.json"), rows, definition,
        file_hash(definition_path),
    )
    manifest = read_json(args.run / "manifest.json")
    run_summary = read_json(args.run / "summary.json")
    config = manifest["config"]
    if (
        config["model_id"] != "Qwen/Qwen3.5-4B"
        or manifest["config_sha256"] != digest(config)
        or manifest["input_sha256"] != digest(rows)
        or config["definition"] != definition
        or load_inputs([args.run / "inputs.json"]) != rows
        or run_summary["state"] != "completed"
        or run_summary["processed"] != 100
        or run_summary["training"] is not False
    ):
        raise ValueError("Frozen 4B manifest/input/completion mismatch")
    baseline = load_completed(args.run, rows, manifest["config_sha256"])
    baseline_score = score_predictions(rows, references, baseline)
    if (baseline_score["matched"], baseline_score["denominator"]) != (88, 95):
        raise ValueError("Original 4B baseline must remain 88/95")
    taxonomy = read_json(TAXONOMY)
    if taxonomy["version"] != "solid-tumor-rules-v3" or taxonomy["engine_version"] != 3:
        raise ValueError("Expected unmodified existing v3 taxonomy")
    protocol = {
        "schema_version": "four-b-existing-rules-quick-v1",
        "methods": METHODS,
        "rule_mapping": RULE_MAPPING,
        "deciding_labels": {key: sorted(value) for key, value in DECIDING_LABELS.items()},
        "strong_fields": ["title", "mesh"],
        "sample_count": 100,
        "scored_count": 95,
        "unscored_count": 5,
        "input_sha256": digest(rows),
        "input_file_sha256": file_hash(args.base / "inputs.json"),
        "reference_sha256": file_hash(args.base / "astra-reference-v1.json"),
        "definition_sha256": file_hash(definition_path),
        "taxonomy_version": taxonomy["version"],
        "taxonomy_file_sha256": file_hash(TAXONOMY),
        "taxonomy_sha256": digest(taxonomy),
        "classifier_sha256": file_hash(ROOT / "apps/curation/classification.py"),
        "evaluation_script_sha256": file_hash(Path(__file__)),
        "baseline_manifest_sha256": file_hash(args.run / "manifest.json"),
        "baseline_results_sha256": digest(baseline),
        "constraints": [
            "Protocol saved before running rules; no new or modified regexes or mappings.",
            "Apply every scheme to all 100 papers, including the 5 unscored reference cases.",
            "Unchanged independent Astra model reference; not a human gold standard.",
            "No model inference, training, network, production DB access, or writes to existing artifacts.",
            "Existing development set, already inspected: exploratory comparison, not independent validation.",
            "CPU timings cover metadata rules and scheme assembly, not prior 4B model inference.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    protocol_path = args.output / "protocol.json"
    if protocol_path.exists():
        if read_json(protocol_path)["protocol"] != protocol:
            raise ValueError("Existing protocol differs; do not overwrite a frozen experiment")
    else:
        save(protocol_path, {"created_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol})

    # Configure only the in-memory offline environment; never import project settings/.env.
    setup_helpers()
    from apps.curation.classification import classify_metadata
    from django.db import connections

    def evaluate():
        started_wall, started_cpu = time.perf_counter(), time.process_time()
        predictions = {method: {} for method in METHODS}
        cases = []
        for row in rows:
            pmid = row["pmid"]
            original = baseline[pmid]
            rule = classify_metadata(row["metadata"], taxonomy)
            for label in rule["labels"]:
                for quote in label["matches"]:
                    validate_quote(quote, row["metadata"])
            for quote in rule.get("suppressed_matches", []):
                validate_quote(quote, row["metadata"])
            mapped = RULE_MAPPING.get(rule["scope"])
            deciding_matches = [
                {"rule_id": label["id"], **quote}
                for label in rule["labels"]
                if label["id"] in DECIDING_LABELS.get(mapped, set())
                for quote in label["matches"]
                if quote["field"] in {"title", "mesh"}
            ]
            use_rule = {
                "A": False,
                "B": mapped is not None,
                "C": original["category"] == "non_neoplastic" and mapped is not None,
                "D": mapped is not None and bool(deciding_matches),
            }
            choices = {}
            for method, override in use_rule.items():
                category = mapped if override else original["category"]
                prediction = {
                    **{key: row[key] for key in IDENTITY_FIELDS},
                    "category": category,
                    "status": "classified" if override else original["status"],
                    "rule_selected": override,
                    "changed": category != original["category"],
                }
                predictions[method][pmid] = prediction
                choices[method] = prediction
            cases.append({
                **{key: row[key] for key in IDENTITY_FIELDS},
                "title": row["metadata"]["title"],
                "reference": references[pmid]["category"],
                "reference_reason": references[pmid]["reason"],
                "reference_evidence": references[pmid]["evidence"],
                "baseline": original["category"],
                "rule_mapped": mapped,
                "strong_deciding_matches": deciding_matches,
                "rule": rule,
                "predictions": choices,
            })
        timing = {"wall_seconds": time.perf_counter() - started_wall,
                  "cpu_seconds": time.process_time() - started_cpu}
        summaries = {}
        for method, results in predictions.items():
            score = score_predictions(rows, references, results)
            improved, regressed, changed = [], [], []
            false_inclusions, new_false_inclusions = [], []
            for case in cases:
                pmid, reference = case["pmid"], case["reference"]
                before, after = case["baseline"], results[pmid]["category"]
                if before != after:
                    changed.append(pmid)
                if reference is None:
                    continue
                if before != reference and after == reference:
                    improved.append(pmid)
                if before == reference and after != reference:
                    regressed.append(pmid)
                if reference == "non_neoplastic" and after in DECIDING_LABELS:
                    false_inclusions.append(pmid)
                    if before not in DECIDING_LABELS:
                        new_false_inclusions.append(pmid)
            summaries[method] = {
                "label": METHODS[method], **score,
                "accuracy": score["matched"] / score["denominator"],
                "delta_accuracy_pp": (score["matched"] - baseline_score["matched"]) / 95 * 100,
                "changed_pmids": changed,
                "improved_pmids": improved,
                "regressed_pmids": regressed,
                "false_inclusions_pmids": false_inclusions,
                "new_false_inclusions_pmids": new_false_inclusions,
                "selected_by_rule_count": sum(row["rule_selected"] for row in results.values()),
            }
        return {"methods": summaries, "cases": cases}, timing

    evaluation, timing = evaluate()
    repeated, repeat_timing = evaluate()
    if evaluation != repeated:
        raise ValueError("Offline repeat changed predictions/hits/metrics")
    if any(connection.connection is not None for connection in connections.all()):
        raise ValueError("Unexpected database connection during pure rule evaluation")
    evaluation.update({
        "protocol_sha256": file_hash(protocol_path),
        "timing": {"first_pass": timing, "repeat": repeat_timing},
        "validation": {
            "unique_inputs": 100,
            "baseline_unchanged": "88/95",
            "all_rule_quote_offsets_verified": True,
            "repeat_results_identical_excluding_timing": True,
            "database_connections_opened": 0,
            "gpu_inference_runs": 0,
            "network_requests": 0,
            "reference_unchanged": file_hash(args.base / "astra-reference-v1.json") == protocol["reference_sha256"],
        },
    })
    save(args.output / "evaluation.json", evaluation)
    save(args.output / "validation.json", evaluation["validation"])
    print(json.dumps({"methods": evaluation["methods"], "timing": evaluation["timing"],
                      "validation": evaluation["validation"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
