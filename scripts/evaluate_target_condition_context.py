"""Compare two source-backed study scopes after the isolated target match."""

import hashlib
import json
from pathlib import Path

from apps.curation.evidence_scope_context import compare_reported_context

PROJECT = Path(__file__).resolve().parents[1]
PREVIEW = PROJECT / "var/experiments/target-identity-scope-20261112-v1/target-preview-astra-reviewed-v1.json"
RUNTIME = Path("/tmp/biomed-two-target-runtime-v2.json")
STORAGE = Path("/tmp/biomed-two-target-memory-storage-v3.json")
OUTPUT = Path("/tmp/biomed-two-target-context-v4.json")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def requirement(item, *, field, expected, known_values, alias):
    companion = item["biomed_companion"]
    fact = companion["fact"]
    anchors = [a for a in companion["anchors"] if (a.get("attributes") or {}).get("alias") == alias]
    if (len(anchors) != 1 or alias not in fact["evidence"]
            or expected.casefold() not in anchors[0]["quote"].casefold()):
        raise ValueError("Context value is not in the selected source anchor")
    fact_text = " ".join([str(fact.get("population") or ""), *(fact.get("conditions") or [])])
    if expected.casefold() not in fact_text.casefold():
        raise ValueError("Context is absent from the frozen fact")
    return {"field": field, "expected": expected, "known_values": known_values,
            "source": {"pmid": item["pmid"], "passage": alias, "locator": anchors[0]["locator"]}}


def evaluate():
    preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8"))
    storage = json.loads(STORAGE.read_text(encoding="utf-8"))
    if (runtime["preview_sha256"] != sha(PREVIEW) or storage["preview_sha256"] != sha(PREVIEW)
            or preview["submit_ready"] or runtime["target_written"] or storage["production_written"]):
        raise ValueError("Experiment inputs differ from the frozen, unsubmitted preview")
    records = {row["pmid"]: row for row in preview["records"]}
    results = {row["pmid"]: row for row in runtime["results"]}
    stored = {row["pmid"]: row for row in storage["results"]}
    plans = (
        ("42719457", "therapy_line", "first-line therapy", ["first-line therapy", "second-line therapy"], "P5",
         {"matching": "first-line therapy", "different": "second-line therapy", "missing": None}, "synthetic_line_matches"),
        ("42756523", "histology", "low-grade serous ovarian carcinoma",
         ["low-grade serous ovarian carcinoma", "high-grade serous ovarian carcinoma"], "P1",
         {"matching": "low-grade serous ovarian carcinoma", "different": "high-grade serous ovarian carcinoma", "missing": None},
         "synthetic_ovarian_subtype_matches"),
    )
    output = []
    for pmid, field, expected, known, alias, scenarios, target_field in plans:
        source = requirement(records[pmid], field=field, expected=expected, known_values=known, alias=alias)
        observed = {label: compare_reported_context([source], {field: value}) for label, value in scenarios.items()}
        statuses = {label: result["status"] for label, result in observed.items()}
        if statuses != {"matching": "consistent_with_reported_context", "different": "different_from_reported_context",
                        "missing": "unknown_context"}:
            raise AssertionError("Unexpected study-context classification")
        baseline = stored[pmid]["consumers"][target_field]
        if baseline != results[pmid][target_field]:
            raise AssertionError("Actual ORM and mocked isolation disagree on target context behavior")
        if len(baseline) != 2 or set(baseline.values()) != {1}:
            raise AssertionError("Target matcher did not return both context scenarios")
        output.append({"pmid": pmid, "source_backed_requirement": source,
                       "isolated_target_context_matches": baseline, "biomed_context_comparison": observed})
    return {"version": "biomed.target-context-comparison.v1", "preview_sha256": sha(PREVIEW),
            "isolated_runtime_sha256": sha(RUNTIME), "memory_storage_sha256": sha(STORAGE),
            "method_sha256": sha(PROJECT / "apps/curation/evidence_scope_context.py"),
            "results": output, "informational_retrieval_allowed_for_all_contexts": True,
            "automatic_patient_eligibility": False, "treatment_recommendation": False,
            "integrated_into_background_or_target": False, "target_written": False,
            "note": "Two source-backed requirements were specified for this experiment; automatic extraction and normalization remain unevaluated."}


if __name__ == "__main__":
    if OUTPUT.exists():
        raise FileExistsError("Keep the first result; choose a new version")
    result = evaluate()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"cases": len(result["results"]), "source_backed_contexts": 2,
                      "target_written": result["target_written"]}, ensure_ascii=False))
