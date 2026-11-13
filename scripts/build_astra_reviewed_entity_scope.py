"""Bind independent source assessments to an inactive offline review package."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from apps.curation.astra_assessment_integrity import validate as validate_assessment
from apps.curation.astra_reference_integrity import validate as validate_reference

ROOT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-identity-scope-20261112-v1")
A_PMIDS = {"42719457", "42756523", "42840361", "42864811"}
B_PMIDS = {"42817873", "42784734", "42872089", "42872145"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def build(a_assessment_path, a_reference_path):
    source_path, comp_path, old_comp_path = ROOT / "source-only.json", ROOT / "comparison-v7.json", ROOT / "comparison-v5.json"
    source, comp, old_comp = read(source_path), read(comp_path), read(old_comp_path)
    a, a_ref = read(a_assessment_path), read(a_reference_path)
    b_path, b_ref_path = ROOT / "astra-assessment-b-v5.json", ROOT / "astra-source-reference-b-exact-v2.json"
    b, b_ref = read(b_path), read(b_ref_path)
    if (a.get("comparison_sha256") != sha(comp_path) or a.get("frozen_reference_sha256") != sha(a_reference_path)
            or b.get("comparison_sha256") != sha(old_comp_path) or b.get("frozen_reference_sha256") != sha(b_ref_path)):
        raise ValueError("Assessment does not bind to the expected comparison and source reference")
    old_rows = {str(row["pmid"]): row for row in old_comp["records"]}
    new_rows = {str(row["pmid"]): row for row in comp["records"]}
    if any(old_rows.get(pmid) != new_rows.get(pmid) for pmid in B_PMIDS):
        raise ValueError("A previously reviewed record changed")
    checks = {
        "source_reference_a": validate_reference(source, a_ref, expected_pmids=A_PMIDS),
        "source_reference_b": validate_reference(source, b_ref, expected_pmids=B_PMIDS),
        "assessment_a_v7": validate_assessment(source, comp, a, expected_pmids=A_PMIDS),
        "assessment_b_carried_forward": validate_assessment(source, comp, b, expected_pmids=B_PMIDS),
        "comparison_v7_sha256": sha(comp_path), "comparison_v5_sha256": sha(old_comp_path),
        "source_only_sha256": sha(source_path), "reference_a_sha256": sha(a_reference_path),
        "reference_b_sha256": sha(b_ref_path),
        "b_reviewed_rows_identical_v5_to_v7": sorted(B_PMIDS),
        "semantic_accuracy_proved": False,
    }
    preview = read(ROOT / "target-preview-unreviewed-v4.json")
    if (preview["comparison_sha256"] != sha(comp_path) or preview["submit_ready"]
            or preview["target_database_connected"] or preview["target_written"]):
        raise ValueError("Preview differs from final inactive offline comparison")
    assessments = {str(item["pmid"]): item for item in [*a["records"], *b["records"]]}
    reviewed = copy.deepcopy(preview)
    for item in reviewed["records"]:
        assessment = assessments[str(item["pmid"])]
        if (assessment["group_id"] != item["group_id"] or item["target_payload"].get("is_active") is not False
                or item["automatic_clinical_matching"] or not item["target_contract_validation"]["valid"]):
            raise ValueError("Candidate differs from reviewed inactive preview")
        item["semantic_review_status"] = ("astra_source_supported" if assessment["core_judgment"] == "supported"
                                           else "astra_source_" + assessment["core_judgment"])
        item["independent_astra_assessment"] = copy.deepcopy(assessment)
    reviewed["version"] = "biomed.entity-scope-astra-reviewed-preview.v1"
    reviewed["source_reference"] = "independent Astra model assessment; not expert gold"
    reviewed["note"] = ("Inactive offline review only. Live target IDs, deduplication, allele matching and "
                        "enforcement of clinical context have not been verified.")
    reviewed["astra_source_supported_count"] = sum(x["semantic_review_status"] == "astra_source_supported"
                                                    for x in reviewed["records"])
    destinations = [ROOT / "astra-source-reference-a-exact-v2.json", ROOT / "astra-assessment-a-v7.json",
                    ROOT / "astra-assessments-validation-v7.json", ROOT / "target-preview-astra-reviewed-v1.json"]
    if any(path.exists() for path in destinations):
        raise FileExistsError("Review artifacts already exist; do not overwrite frozen research")
    for target, original in zip(destinations[:2], (a_reference_path, a_assessment_path), strict=True):
        with target.open("xb") as stream:
            stream.write(original.read_bytes())
    write_new(destinations[2], checks)
    write_new(destinations[3], reviewed)
    return {"records": len(reviewed["records"]), "astra_supported": reviewed["astra_source_supported_count"],
            "source_quotes_verified": sum(checks[k]["source_quotes_verified"] for k in
                                          ("source_reference_a", "source_reference_b", "assessment_a_v7", "assessment_b_carried_forward")),
            "submit_ready": reviewed["submit_ready"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-assessment", type=Path, default=Path("/tmp/biomed-astra-identity-assessment-a-v7-20261112.json"))
    parser.add_argument("--a-reference", type=Path, default=Path("/tmp/biomed-astra-identity-reference-a-20261112.json"))
    args = parser.parse_args()
    print(json.dumps(build(args.a_assessment, args.a_reference), ensure_ascii=False))
