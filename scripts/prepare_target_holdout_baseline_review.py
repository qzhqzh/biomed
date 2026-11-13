"""Prepare a bounded baseline-only review after the blind reference is frozen."""

import argparse
import hashlib
import json
from pathlib import Path

from apps.curation.source_linked_facts import evidence_index
from scripts.knowledge_observation_report import digest

CHALLENGE = {"42825379", "42733285", "42848093"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare(root, output):
    root, output = Path(root), Path(output)
    manifest, samples = read(root / "manifest.json"), read(root / "samples.json")
    reference = read(root / "astra-business-reference.json")
    validation = read(root / "astra-business-reference-validation.json")
    if (digest(samples) != manifest["samples_sha256"] or validation["source_sha256"] != manifest["source_sha256"]
            or validation["reference_sha256"] != hashlib.sha256((root / "astra-business-reference.json").read_bytes()).hexdigest()):
        raise ValueError("Frozen samples or source reference differ")
    pmids = {str(row["pmid"]) for row in reference["records"]
             if row["business_scope"] == "in_scope_primary_human_tumor"} | CHALLENGE
    if len(pmids) != 10:
        raise ValueError("Unexpected frozen reference scope")
    records = []
    for sample in samples:
        if sample["pmid"] not in pmids:
            continue
        if digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]:
            raise ValueError(f"Frozen sample differs: {sample['group_id']}")
        record = sample["record"]
        index = evidence_index(record, sample["source_document"])
        records.append({"pmid": sample["pmid"], "group_id": sample["group_id"],
                        "sampling_group": sample["sampling_group"], "fact": record.get("fact"),
                        "entities": [{"ref": e["ref"], "kind": e["kind"], "label": e["label"]}
                                     for e in record.get("entities", [])],
                        "evidence": [{"alias": alias, **value} for alias, value in index.items()],
                        "old_offline_ready": bool(sample["original_export"].get("offline_ready")),
                        "old_reason": sample["reason_code"], "sample_sha256": sample["sample_sha256"]})
    if {r["pmid"] for r in records} != pmids:
        raise ValueError("Baseline candidate selection is incomplete")
    value = {"version": "target-holdout-baseline-review-packet-v1", "source_sha256": manifest["source_sha256"],
             "source_reference_sha256": validation["reference_sha256"], "articles": len(pmids),
             "candidate_count": len(records), "new_model_outputs_included": False, "records": records}
    if output.exists():
        raise FileExistsError("Frozen candidate review packet already exists")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return {"articles": len(pmids), "candidate_count": len(records), "packet_sha256": digest(value)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.output), ensure_ascii=False))
