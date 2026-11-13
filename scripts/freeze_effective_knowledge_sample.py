"""Freeze a small, source-backed target-yield comparison without touching production.

The holdout is deliberately stored separately and never printed. The queue is
opened read-only; original batches, models and source material are not modified.
"""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.decision import digest as source_digest
from scripts.knowledge_observation_report import (
    DEFAULT_ROOT,
    batch_path,
    digest,
    read,
    verified_batch,
)

VERSION = "effective-knowledge-sample-v1"
SEED = "biomed-effective-knowledge-20261112-v1"
DEVELOPMENT_CASES = {
    "E-42836110-0002": "mapping_needed",
    "E-42838582-0012": "outside_target",
}


def selection_order(row):
    key = "|".join((SEED, row["batch_id"], row["pmid"], row["group_id"]))
    return hashlib.sha256(key.encode()).hexdigest()


def select_sample(rows):
    buckets = {name: [] for name in ("offline_ready", "mapping_needed", "outside_target", "extraction_gap")}
    for row in rows:
        if row.get("status") in buckets and row.get("pmid") and row.get("group_id") and row.get("batch_id"):
            buckets[row["status"]].append({**row, "pmid": str(row["pmid"])})
    for values in buckets.values():
        values.sort(key=selection_order)
    if len(buckets["offline_ready"]) != 3:
        raise ValueError("Expected the three frozen offline-ready controls; refresh the sampling definition if changed")
    development = [{**row, "sample_role": "development"} for row in buckets["offline_ready"]]
    used = {row["pmid"] for row in development}
    if len(used) != 3:
        raise ValueError("Offline-ready controls must come from three different articles")
    for group, status in DEVELOPMENT_CASES.items():
        matches = [row for row in buckets[status] if row["group_id"] == group]
        if len(matches) != 1 or matches[0]["pmid"] in used:
            raise ValueError(f"Known development case is missing or ambiguous: {group}")
        development.append({**matches[0], "sample_role": "development"})
        used.add(matches[0]["pmid"])

    def add_unique(target, status, count, role):
        for row in buckets[status]:
            if row["pmid"] in used:
                continue
            target.append({**row, "sample_role": role})
            used.add(row["pmid"])
            count -= 1
            if count == 0:
                return
        raise ValueError(f"Not enough distinct articles for {role}: {status}")

    holdout = []
    for status, count in (("mapping_needed", 3), ("outside_target", 3), ("extraction_gap", 4)):
        add_unique(holdout, status, count, "holdout")
    for status, count in (("mapping_needed", 5), ("outside_target", 5), ("extraction_gap", 5)):
        add_unique(development, status, count, "development")
    if len(development) != 20 or len(holdout) != 10 or len(used) != 30:
        raise ValueError("Sample size or article separation failed")
    return development, holdout


def queue_rows(root):
    uri = (root / "queue.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        data = list(db.execute("SELECT id, summary FROM batches WHERE state='completed' ORDER BY created,id"))
    rows = []
    for batch_id, summary_json in data:
        summary = json.loads(summary_json)
        for item in summary.get("target_readiness", {}).get("records", []):
            rows.append({"batch_id": batch_id, "pmid": str(item.get("pmid") or ""),
                         "group_id": item.get("group_id"), "status": item.get("status"),
                         "reason_code": item.get("reason_code")})
    return rows


def materialize(root, selected):
    by_batch = {}
    samples = []
    for item in selected:
        batch_id = item["batch_id"]
        if batch_id not in by_batch:
            folder = batch_path(root, batch_id)
            records, exports, candidates, _ = verified_batch(root, {"id": batch_id})
            source = read(folder / "sources/snapshot.json")
            if source.get("sha256") != source_digest(source.get("snapshot")):
                raise ValueError(f"Invalid source snapshot digest: {batch_id}")
            by_batch[batch_id] = {
                "records": {r["group_id"]: r for r in records},
                "exports": {r["group_id"]: r for r in exports},
                "candidates": candidates,
                "mappings": read(folder / "final/target-mappings.json"),
                "source": source,
                "manifest": read(folder / "final/source-manifest.json"),
            }
        batch = by_batch[batch_id]
        group = item["group_id"]
        record = batch["records"][group]
        if str(record.get("article", {}).get("pmid")) != item["pmid"]:
            raise ValueError(f"Article identity differs for {group}")
        docs = [d for d in batch["source"]["snapshot"]["documents"] if str(d.get("pmid")) == item["pmid"]]
        if len(docs) != 1 or not docs[0].get("fulltext_version_id"):
            raise ValueError(f"Fulltext source missing or ambiguous for {group}")
        doc = docs[0]
        if doc.get("passages_sha256") != source_digest(doc.get("passages")):
            raise ValueError(f"Fulltext passage digest differs for {group}")
        if str(doc["fulltext_version_id"]).replace("-", "") != str(record["version_id"]).replace("-", ""):
            raise ValueError(f"Fulltext version differs for {group}")
        exported = dict(batch["exports"][group])
        exported.pop("biomed_companion", None)
        sample = {**item, "candidate_id": batch["candidates"][group], "record": record,
                  "original_export": exported, "original_mapping": batch["mappings"][group],
                  "source_document": doc, "source_snapshot_sha256": batch["source"]["sha256"],
                  "source_manifest": batch["manifest"]}
        sample["sample_sha256"] = digest(sample)
        samples.append(sample)
    return samples


def freeze(root, output):
    root, output = Path(root), Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing sample: {output}")
    development, holdout = select_sample(queue_rows(root))
    dev_data, hold_data = materialize(root, development), materialize(root, holdout)
    manifest = {"version": VERSION, "seed": SEED, "created_at": datetime.now(timezone.utc).isoformat(),
                "counts": {"development": len(dev_data), "holdout": len(hold_data),
                           "status": dict(Counter(r["status"] for r in dev_data + hold_data))},
                "development_sha256": digest(dev_data), "holdout_sha256": digest(hold_data),
                "selection": "Machine status used only for stratification; separate PMID; not a gold label",
                "target_contacted": False, "production_database_written": False}
    output.mkdir(parents=True, exist_ok=False)
    for name, data in (("development.json", dev_data), ("holdout.json", hold_data), ("manifest.json", manifest)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = freeze(args.root, args.output)
    print(json.dumps({"output": str(args.output), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
