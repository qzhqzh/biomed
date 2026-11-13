"""Preserve a read-only duplicate-target observation without selecting an ID."""

import argparse
import hashlib
import json
from pathlib import Path

RXNORM = Path("/appdata/02_resource/apps/biomed/resources/drug-translations-20261025/rxnorm-ingredients.json")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(root, output):
    root, output = Path(root), Path(output)
    if output.exists():
        raise FileExistsError("Preserve the original target identity observation")
    observation = root / "target-lorlatinib-duplicate-readonly.json"
    target = json.loads(observation.read_text(encoding="utf-8"))
    ingredients = json.loads(RXNORM.read_text(encoding="utf-8"))
    exact = [row for row in ingredients if row.get("identifier") == "RXNORM:2103164"]
    if (len(exact) != 1 or exact[0].get("en", "").casefold() != "lorlatinib"
            or not target.get("transaction_read_only") or target.get("target_written")
            or target.get("patient_data_queried") or not target.get("same_ids_as_snapshot")
            or len(target.get("drugs", [])) != 2
            or {row["name_en"].casefold() for row in target["drugs"]} != {"lorlatinib"}):
        raise ValueError("Local standard or target observation differs")
    result = {
        "version": "target-drug-identity-observation-v1",
        "local_ingredient": {"system": "RxNorm", "id": "2103164", "name_en": "lorlatinib"},
        "target_entries": target["drugs"],
        "evidence_center_counts": target["evidence_center_counts"],
        "legacy_tumor_drug_counts": target["legacy_tumor_drug_counts"],
        "target_observation_sha256": sha(observation), "rxnorm_snapshot_sha256": sha(RXNORM),
        "published_name_reference": "https://pharm.ncmi.cn/zysjk/ypjkyp/202312/t20231205_399511.html",
        "meaning": "One local ingredient has two exact-English target records; keep both links as alternatives.",
        "export_id_selected": False,
        "reason": "Both IDs have legacy references; consumer-wide equivalence and duplicate checking remain unverified.",
        "local_registry_changed": False, "target_written": False,
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"target_records": len(result["target_entries"]), "export_id_selected": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output), ensure_ascii=False))
