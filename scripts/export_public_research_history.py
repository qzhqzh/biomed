"""Create portable research summaries without copying source passages.

Detailed records remain in the local research_data directory. This export keeps
the task, methods, measurements and classification outcomes for a fresh clone.
"""

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.research_history import (
    DIRECTORY,
    PUBLIC_DIRECTORY,
    read_record,
    register_record,
)

CASE_FIELDS = {
    "cohort", "rank", "pmid", "title", "source_scope", "classification_input_scope",
    "reference_state", "reference_relevance", "reference_scope", "acceptable_relevance",
    "scope_eligible", "rule_scope", "rule_original_scope", "rule_status", "model_scope",
    "model_relevance", "model_status", "model_seconds", "rule_outcome", "model_outcome",
    "model_relevance_outcome", "rule_acceptable_scope", "model_acceptable_scope",
    "model_acceptable_relevance", "model_raw_relevance",
}
METRIC_FIELDS = {"label", "value", "unit", "numerator", "denominator", "note"}


def metadata_summary(record, source_sha256):
    """Use an allowlist so newly added source/evidence fields stay local."""
    result = {
        key: record[key]
        for key in (
            "schema_version", "id", "title", "date", "stage", "status", "sample_count",
            "sample_unit", "summary", "task_kind", "identity",
        )
        if key in record
    }
    result.update(
        task={
            key: record["task"][key]
            for key in ("question", "outputs", "not_measured")
            if key in record["task"]
        },
        dataset={
            key: record["dataset"][key]
            for key in ("label", "role", "document_count")
            if key in record["dataset"]
        },
        reference={
            key: record["reference"][key]
            for key in ("label", "status", "note")
            if key in record["reference"]
        },
        methods=[
            {
                **{
                    key: method[key]
                    for key in ("id", "label", "version", "conclusion")
                    if key in method
                },
                "metrics": [
                    {key: value for key, value in metric.items() if key in METRIC_FIELDS}
                    for metric in method.get("metrics", [])
                ],
            }
            for method in record["methods"]
        ],
        findings=["公开摘要保留方法和指标；原文片段与逐条证据保存在本机完整记录。"],
        next_steps=record["next_steps"],
        provenance=[
            {
                "label": "本机完整研究记录",
                "artifact": f"apps/curation/research_data/{record['id']}.json",
                "sha256": source_sha256,
            }
        ],
        summary_level="metadata_only",
    )
    if isinstance(record.get("cases"), list):
        result["cases"] = [
            {key: value for key, value in row.items() if key in CASE_FIELDS}
            for row in record["cases"]
            if isinstance(row, dict)
        ]
    return result


def export(source=DIRECTORY, target=PUBLIC_DIRECTORY):
    source, target = Path(source), Path(target)
    outcomes = []
    for path in sorted(source.glob("*.json")):
        record = read_record(path.stem, source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        outcomes.append(register_record(metadata_summary(record, digest), target))
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DIRECTORY)
    parser.add_argument("--target", type=Path, default=PUBLIC_DIRECTORY)
    args = parser.parse_args()
    outcomes = export(args.source, args.target)
    print(f"Research summaries: {len(outcomes)}")


if __name__ == "__main__":
    main()
