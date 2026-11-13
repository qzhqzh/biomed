"""One bounded, read-only pass over completed batch artifacts for new SNV hints."""

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from scripts.freeze_target_holdout_20261113 import LICENSE, PRIOR, excluded_pmids
from scripts.knowledge_observation_report import DEFAULT_ROOT

VARIANT = re.compile(r"(?<![A-Za-z0-9])(?:p\.)?([ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{1,5}[ACDEFGHIKLMNPQRSTVWY*])"
                     r"(?![A-Za-z0-9])")


def scan(root, selection_path):
    root = Path(root)
    selected = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    excluded = excluded_pmids() | {str(row["pmid"]) for row in selected}
    if not PRIOR.is_dir():
        raise ValueError("Prior exclusion manifest is unavailable")
    uri = (root / "queue.sqlite3").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        batch_ids = [row[0] for row in db.execute("SELECT id FROM batches WHERE state='completed' ORDER BY created,id")]
    by_pmid, stats = {}, Counter()
    for batch in batch_ids:
        path = root / "batches" / batch / "final/knowledge.json"
        if not path.is_file():
            stats["missing_batch_artifact"] += 1
            continue
        bundle = json.loads(path.read_text(encoding="utf-8"))
        stats["batches_read"] += 1
        for row in bundle.get("records", []):
            stats["records_considered"] += 1
            article, fact = row.get("article") or {}, row.get("fact") or {}
            pmid = str(article.get("pmid") or "")
            if not pmid or pmid in excluded or not LICENSE.search(str(article.get("rights") or "")):
                continue
            if fact.get("relation") != "treatment_outcome" or not isinstance(fact.get("results"), list) or not fact["results"]:
                continue
            drugs = fact.get("drug_names")
            if not isinstance(drugs, list) or len(drugs) != 1 or not isinstance(drugs[0], str) or not drugs[0].strip():
                continue
            relevant = json.dumps({key: fact.get(key) for key in ("population", "conditions")}, ensure_ascii=False)
            variants = {m.group(1) for m in VARIANT.finditer(relevant)}
            if len(variants) != 1:
                continue
            variant, drug = next(iter(variants)), drugs[0]
            evidence = [str(a.get("quote") or "") for a in row.get("anchors", []) if a.get("purpose") == "evidence"]
            combined = any(variant in value and drug.casefold() in value.casefold() for value in evidence)
            genes = sorted({e.get("label") for e in row.get("entities", [])
                            if e.get("kind") == "gene" and isinstance(e.get("label"), str)})
            hint = {"pmid": pmid, "title": article.get("title"), "batch_id": batch, "group_id": row["group_id"],
                    "variant": variant, "drug": drug, "gene_labels": genes, "same_evidence_span": combined,
                    "source_sha256": row.get("source_sha256"), "version_id": row.get("version_id")}
            score = 4 * combined + 2 * bool(genes) + bool(fact.get("population"))
            previous = by_pmid.get(pmid)
            if previous is None or score > previous[0] or (score == previous[0] and hint["group_id"] < previous[1]["group_id"]):
                by_pmid[pmid] = (score, hint)
        # Do not compete continuously with the resident process for disk reads.
        time.sleep(.2)
    candidates = [item for _, item in sorted(by_pmid.values(), key=lambda pair: (-pair[0], pair[1]["pmid"]))]
    return {"version": "target-snv-replay-hints-v1", "stats": dict(stats), "candidate_article_count": len(candidates),
            "note": "Old model fields and lexical matches are hints only, not clinical or target acceptance.",
            "target_contacted": False, "database_written": False, "candidates": candidates}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = scan(args.root, args.selection)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"stats": result["stats"], "candidate_article_count": result["candidate_article_count"]}))
