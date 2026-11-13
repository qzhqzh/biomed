"""Offline, source-checked evaluation of the frozen three-category task."""

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from apps.curation.decision import checked_input, digest
from apps.curation.evidence_pilot import load_snapshot
from apps.curation.three_class import CATEGORIES, load_completed, load_inputs

VERSION = "three-class-evaluation-v1"
IDENTITY_FIELDS = ("pmid", "version_id", "source_sha256", "input_sha256")
RULE_MAPPING = {
    "solid_candidate": "solid",
    "solid_boundary_candidate": "solid",
    "hematologic_candidate": "hematologic",
}
RULE_SCOPES = set(RULE_MAPPING) | {
    "uncertain",
    "mixed_candidate",
    "benign_candidate",
    "precursor_candidate",
}


def read_json(path):
    return json.loads(Path(path).read_text())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_quote(quote, metadata):
    field = quote.get("field")
    if field not in metadata:
        raise ValueError("Reference/rule quote uses unknown field")
    text = metadata[field]
    if isinstance(text, list):
        text = " ; ".join(text)
    start, end, value = quote.get("start"), quote.get("end"), quote.get("text")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(text)
        or not isinstance(value, str)
        or text[start:end] != value
    ):
        raise ValueError("Reference/rule quote does not match exact source offsets")
    return {key: quote[key] for key in ("field", "text", "start", "end")}


def validate_reference(reference, rows, definition, definition_sha256):
    expected = {
        "schema_version": "three-class-reference-v1",
        "annotator_model": "gpt-6-astra",
        "status": "model_reference_not_human_gold",
        "blinded_to_candidates": True,
        "input_scope": "title_abstract_mesh_publication_types",
        "task_version": definition["task_version"],
        "definition_sha256": definition_sha256,
        "evidence_offsets": "zero_based_unicode_codepoints_end_exclusive",
    }
    if (
        any(reference.get(key) != value for key, value in expected.items())
        or reference.get("blinded_to_candidates") is not True
    ):
        raise ValueError("Reference model/blinding/task/definition metadata mismatch")
    if not isinstance(reference.get("created_at"), str) or not reference["created_at"]:
        raise ValueError("Missing reference creation time")
    sources = {row["pmid"]: row for row in rows}
    checked = {}
    for row in reference["records"]:
        pmid = row.get("pmid")
        source = sources.get(pmid)
        if (
            pmid in checked
            or source is None
            or any(row.get(key) != source[key] for key in IDENTITY_FIELDS)
        ):
            raise ValueError("Duplicate or mismatched reference source identity")
        category, state = row.get("category"), row.get("reference_state")
        if not (
            (state == "determinate" and category in CATEGORIES)
            or (state == "unresolved" and category is None)
        ):
            raise ValueError("Reference category/state mismatch")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Missing reference reason")
        if not isinstance(row.get("evidence"), list) or not row["evidence"]:
            raise ValueError("Missing reference evidence")
        checked[pmid] = {
            **row,
            "evidence": [
                validate_quote(quote, source["metadata"]) for quote in row["evidence"]
            ],
        }
    if set(checked) != set(sources):
        raise ValueError("Missing reference records")
    return checked


def load_rules(snapshot_folders, rows):
    sources = {row["pmid"]: row for row in rows}
    documents, snapshots = {}, []
    for folder in snapshot_folders:
        frozen = load_snapshot(folder)
        snapshot = frozen["snapshot"]
        taxonomy = snapshot["taxonomy"]
        if (
            taxonomy.get("version") != "solid-tumor-rules-v3"
            or taxonomy.get("engine_version") != 3
            or digest(taxonomy) != snapshot["taxonomy_sha256"]
        ):
            raise ValueError("Expected frozen rules v3 taxonomy")
        vocabulary = {(rule["axis"], rule["id"]) for rule in taxonomy["rules"]}
        snapshots.append(
            {
                "path": str(Path(folder) / "snapshot.json"),
                "sha256": file_hash(Path(folder) / "snapshot.json"),
                "snapshot_sha256": frozen["sha256"],
                "taxonomy_sha256": snapshot["taxonomy_sha256"],
            }
        )
        for document in snapshot["documents"]:
            source = checked_input(document["model_input"])
            pmid = source["pmid"]
            if pmid in documents or sources.get(pmid) != source:
                raise ValueError("Rule snapshot source mismatch or duplicate")
            baseline = document["rule_baseline"]
            if baseline["scope"] not in RULE_SCOPES:
                raise ValueError("Unknown frozen rule scope")
            labels = []
            for label in baseline["labels"]:
                if (label["axis"], label["id"]) not in vocabulary:
                    raise ValueError("Unknown frozen rule label")
                labels.append(
                    {
                        **{key: label[key] for key in ("axis", "id", "label")},
                        "matches": [
                            validate_quote(q, source["metadata"])
                            for q in label["matches"]
                        ],
                    }
                )
            documents[pmid] = {
                "scope": RULE_MAPPING.get(baseline["scope"]),
                "original_scope": baseline["scope"],
                "labels": labels,
                "source_scope": document["input_scope"],
            }
    if set(documents) != set(sources):
        raise ValueError("Missing rule snapshots for inputs")
    return documents, snapshots


def outcome(prediction, reference, status):
    if reference is None:
        return "unscored"
    if status not in ("classified", "abstained"):
        return "failed"
    if prediction is None:
        return "abstained"
    return "matched" if prediction == reference else "disagreed"


def method_summary(cases, prefix):
    outcomes = Counter(row[f"{prefix}_outcome"] for row in cases)
    by_class = {}
    confusion = {
        category: {name: 0 for name in (*CATEGORIES, "abstained", "failed")}
        for category in CATEGORIES
    }
    for category in CATEGORIES:
        subset = [row for row in cases if row["reference_relevance"] == category]
        by_class[category] = {
            "matched": sum(row[f"{prefix}_outcome"] == "matched" for row in subset),
            "denominator": len(subset),
        }
        for row in subset:
            bucket = row[f"{prefix}_outcome"]
            if bucket not in ("abstained", "failed"):
                bucket = row[f"{prefix}_scope"]
            confusion[category][bucket] += 1
    completed = sum(row[f"{prefix}_scope"] in CATEGORIES for row in cases)
    statuses = Counter(row[f"{prefix}_status"] for row in cases)
    macro_recall = (
        sum(item["matched"] / item["denominator"] for item in by_class.values())
        / len(CATEGORIES)
        if all(item["denominator"] for item in by_class.values())
        else None
    )
    return {
        "matched": outcomes["matched"],
        "denominator": len(cases) - outcomes["unscored"],
        "disagreed": outcomes["disagreed"],
        "abstained": outcomes["abstained"],
        "failed": outcomes["failed"],
        "unscored": outcomes["unscored"],
        "completed": completed,
        "total": len(cases),
        "statuses": dict(statuses),
        "by_class": by_class,
        "confusion": confusion,
        "macro_recall": macro_recall,
    }


def evaluate(folder, snapshot_folders):
    folder = Path(folder)
    rows = load_inputs([folder / "inputs.json"])
    if len(rows) != 30:
        raise ValueError("This experiment requires the frozen 30 documents")
    definition_path = folder / "task-definition-v1.json"
    definition = read_json(definition_path)
    if definition.get("categories") != list(CATEGORIES):
        raise ValueError("Unexpected business categories")
    reference_path = folder / "astra-reference-v1.json"
    reference = read_json(reference_path)
    references = validate_reference(
        reference, rows, definition, file_hash(definition_path)
    )
    manifest_path = folder / "run/manifest.json"
    manifest = read_json(manifest_path)
    if (
        digest(manifest["config"]) != manifest["config_sha256"]
        or manifest["config"].get("model_id") != "Qwen/Qwen3-1.7B"
        or manifest["config"].get("definition") != definition
        or manifest["config"].get("prompt") != definition["prompt"]
        or manifest["records"] != len(rows)
        or manifest["input_sha256"] != digest(rows)
        or load_inputs([folder / "run/inputs.json"]) != rows
    ):
        raise ValueError("Model manifest/input/definition mismatch")
    results = load_completed(folder / "run", rows, manifest["config_sha256"])
    if set(results) != {row["pmid"] for row in rows}:
        raise ValueError("Missing model results; do not silently drop failures")
    rules, snapshots = load_rules(snapshot_folders, rows)
    cases = []
    for rank, row in enumerate(rows, 1):
        pmid = row["pmid"]
        ref, result, baseline = references[pmid], results[pmid], rules[pmid]
        status = result["status"]
        rule_status = "classified" if baseline["scope"] is not None else "abstained"
        cases.append(
            {
                "cohort": "pilot30",
                "rank": rank,
                "pmid": pmid,
                "title": row["metadata"]["title"],
                "source_scope": baseline["source_scope"],
                "classification_input_scope": definition["input_scope"],
                "reference_state": ref["reference_state"],
                "reference_relevance": ref["category"],
                "reference_scope": ref["category"],
                "reference_notes": ref["reason"],
                "reference_quotes": ref["evidence"],
                "reference_labels": [],
                "acceptable_relevance": [],
                "scope_eligible": ref["category"] is not None,
                "rule_scope": baseline["scope"],
                "rule_original_scope": baseline["original_scope"],
                "rule_labels": baseline["labels"],
                "rule_status": rule_status,
                "model_scope": result["category"],
                "model_relevance": result["category"],
                "model_status": "validated_candidate"
                if status == "classified"
                else status,
                "model_error": result.get("error", ""),
                "model_seconds": result["seconds"],
                "model_labels": [],
                "rule_outcome": outcome(
                    baseline["scope"], ref["category"], rule_status
                ),
                "model_outcome": outcome(result["category"], ref["category"], status),
                "model_relevance_outcome": outcome(
                    result["category"], ref["category"], status
                ),
            }
        )
    summary = read_json(folder / "run/summary.json")
    seconds = sum(result["seconds"] for result in results.values())
    if (
        summary.get("state") != "completed"
        or summary.get("processed") != 30
        or summary.get("total") != 30
        or not math.isfinite(seconds)
        or round(seconds, 3) != summary.get("inference_seconds")
    ):
        raise ValueError("Model timing/completion summary mismatch")
    always_solid_cases = [
        {
            **row,
            "always_solid_scope": "solid",
            "always_solid_status": "classified",
            "always_solid_outcome": outcome(
                "solid", row["reference_relevance"], "classified"
            ),
        }
        for row in cases
    ]
    methods = {name: method_summary(cases, name) for name in ("rule", "model")}
    methods["always_solid"] = method_summary(always_solid_cases, "always_solid")
    return {
        "schema_version": VERSION,
        "task_kind": "three_class",
        "task_version": definition["task_version"],
        "sample_count": 30,
        "input_sha256": digest(rows),
        "definition_sha256": file_hash(definition_path),
        "reference_sha256": file_hash(reference_path),
        "manifest_sha256": file_hash(manifest_path),
        "config_sha256": manifest["config_sha256"],
        "reference_metadata": {
            key: value for key, value in reference.items() if key != "records"
        },
        "definition": definition,
        "snapshot_sources": snapshots,
        "methods": methods,
        "timing": {
            "inference_seconds": round(seconds, 3),
            "mean_seconds": round(seconds / 30, 3),
            "load_seconds": summary["load_seconds"],
            "peak_allocated_mib": summary["peak_allocated_mib"],
        },
        "records": cases,
    }
