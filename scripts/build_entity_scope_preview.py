"""Package new source-linked target previews without importing them."""

import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-identity-scope-20261112-v1")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(comparison_path):
    comparison_path = Path(comparison_path)
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    code = PROJECT / "apps/curation/source_linked_target_repair.py"
    if comparison["repair_code_sha256"] != sha(code):
        raise ValueError("Repair implementation differs from frozen comparison")
    records = []
    for row in comparison["records"]:
        candidate = row["new_preview"]
        if candidate["status"] != "offline_structure_only":
            continue
        export = candidate["derived_export"]
        payload = export["target_payload"]
        if (not export["offline_ready"] or export["submit_ready"] or payload.get("is_active") is not False
                or candidate["automatic_clinical_matching"] or not export["contract_validation"]["valid"]):
            raise ValueError("Only inactive offline-validated candidates may enter this preview")
        companion = export["biomed_companion"]
        records.append({"pmid": row["pmid"], "group_id": row["group_id"],
                        "target_payload": payload, "target_contract_validation": export["contract_validation"],
                        "matching_validation": export["matching_validation"], "biomed_companion": companion,
                        "semantic_review_status": "awaiting_independent_astra_review",
                        "target_live_dictionary_verified": False,
                        "automatic_clinical_matching": False,
                        "notes": export["semantic_limits"]})
    if len(records) != comparison["new_offline_candidates"]:
        raise ValueError("Preview count differs from frozen comparison")
    return {"version": "biomed.entity-scope-target-preview.v1", "record_count": len(records),
            "comparison_sha256": sha(comparison_path), "dictionary_sha256": comparison["dictionary_sha256"],
            "records": records, "target_database_connected": False, "target_written": False,
            "submit_ready": False, "note": "Inactive local review package; source and necessary conditions stay in companion and description."}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", type=Path, default=ROOT / "comparison-v3.json")
    parser.add_argument("--output", type=Path, default=ROOT / "target-preview-unreviewed-v1.json")
    args = parser.parse_args()
    output = args.output
    result = build(args.comparison)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"record_count": result["record_count"], "submit_ready": result["submit_ready"]}, ensure_ascii=False))
