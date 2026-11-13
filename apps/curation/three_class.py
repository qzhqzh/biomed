"""Pure validation and checkpoint helpers for the isolated three-class experiment."""

import json
from collections import Counter
from pathlib import Path

from apps.curation.decision import checked_input, digest

VERSION = "literature-three-class-v1"
CATEGORIES = ("solid", "hematologic", "non_neoplastic")
RESULT_STATUSES = {
    "classified",
    "abstained",
    "invalid_output",
    "output_truncated",
    "input_too_long",
}


def load_inputs(paths):
    rows = [
        checked_input(row)
        for path in paths
        for row in json.loads(Path(path).read_text())["records"]
    ]
    if not rows or len({row["pmid"] for row in rows}) != len(rows):
        raise ValueError("Empty or duplicate input records")
    return rows


def validate_output(raw):
    """Validate only the requested category; abstention is a processing state."""
    value = json.loads(raw.strip())
    if not isinstance(value, dict) or set(value) != {"category"}:
        raise ValueError("Expected one JSON object with only category")
    category = value["category"]
    if category is not None and category not in CATEGORIES:
        raise ValueError("Unknown category")
    return category


def load_completed(folder, rows, config_sha256):
    allowed = {row["pmid"]: row for row in rows}
    completed = {}
    for path in sorted(folder.glob("result-*.json")):
        result = json.loads(path.read_text())
        pmid = result["pmid"]
        if pmid not in allowed or path.name != f"result-{pmid}.json":
            raise ValueError("Unexpected checkpoint PMID/file")
        source = allowed[pmid]
        if (
            result.get("config_sha256") != config_sha256
            or any(
                result.get(key) != source[key]
                for key in ("version_id", "source_sha256", "input_sha256")
            )
            or result.get("status") not in RESULT_STATUSES
        ):
            raise ValueError("Existing result has mismatched provenance/status")
        if result["status"] in ("classified", "abstained"):
            category = validate_output(result["raw_output"])
            if result.get("category") != category or (
                result["status"] == "abstained"
            ) != (category is None):
                raise ValueError("Checkpoint category/status does not match raw output")
        elif result.get("category") is not None:
            raise ValueError("Failed output cannot carry a scored category")
        if not isinstance(result.get("seconds"), (int, float)) or result["seconds"] < 0:
            raise ValueError("Invalid checkpoint timing")
        completed[pmid] = result
    return completed


def summarize(rows, completed):
    results = list(completed.values())
    elapsed = sum(result["seconds"] for result in results)
    return {
        "total": len(rows),
        "processed": len(results),
        "statuses": dict(Counter(result["status"] for result in results)),
        "categories": dict(
            Counter(
                result["category"]
                for result in results
                if result["status"] == "classified"
            )
        ),
        "inference_seconds": round(elapsed, 3),
        "mean_seconds": round(elapsed / len(results), 3) if results else None,
        "training": False,
        "network_requests": 0,
        "input_sha256": digest(rows),
    }
