"""Replay the source-context guard against an already frozen comparison."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from apps.curation.source_study_context import guard
from scripts.knowledge_observation_report import digest as snapshot_digest

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--comparison", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    samples = json.loads(args.samples.read_text(encoding="utf-8"))
    comparison = json.loads(args.comparison.read_text(encoding="utf-8"))
    index = {s["group_id"]: s for s in samples}
    rows = []
    for original in comparison["records"]:
        sample = index[original["group_id"]]
        if (snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["pmid"] != original["pmid"]):
            raise ValueError("Frozen sample differs from comparison")
        result = guard(sample, original)
        rows.append({"group_id": sample["group_id"], "pmid": sample["pmid"],
                     "previous_status": original["status"], "previous_reason": original["reason"],
                     "status": result["status"], "reason": result["reason"], "study_context": result["study_context"]})
    counts = Counter(row["reason"] for row in rows)
    output = {"version": "biomed.source-context-comparison.v1", "samples": len(rows), "reasons": dict(counts),
              "review_primary_conflicts": sum(row["study_context"]["review_primary_conflict"] for row in rows),
              "preclinical_source_populations": sum(row["study_context"]["preclinical_population_explicit_in_fact_and_evidence"] for row in rows),
              "treatment_receipt_results": sum(row["study_context"]["result_describes_treatment_receipt"] for row in rows),
              "comparison_sha256": hashlib.sha256(args.comparison.read_bytes()).hexdigest(),
              "guard_sha256": hashlib.sha256((PROJECT / "apps/curation/source_study_context.py").read_bytes()).hexdigest(),
              "target_written": False, "production_workflow_changed": False, "records": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({k: output[k] for k in ("samples", "review_primary_conflicts", "preclinical_source_populations", "treatment_receipt_results", "reasons")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
