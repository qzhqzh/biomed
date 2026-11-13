"""Freeze a small, article-separated source-first comparison from finished batches.

Inputs are verified existing batch snapshots; no collection, model, application
database write, target connection or target data import is performed.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.decision import digest as source_digest
from scripts.freeze_effective_knowledge_sample import materialize, queue_rows
from scripts.knowledge_observation_report import DEFAULT_ROOT, digest, verified_batch

PROJECT = Path(__file__).resolve().parents[1]
PRIOR = PROJECT / "var/experiments/knowledge-target-optimization-20261112-v1"
SEED = "target-holdout-20261113-v1"
SIGNAL = re.compile(r"\b(?:EGFR|KRAS|BRAF|ALK|ROS1|HER2|BRCA1|BRCA2|FGFR[1-4]|NTRK[1-3]|"
                    r"RET|MET|mutation|mutated|mutant|fusion|genomic|molecular)\b", re.IGNORECASE)
CLINICAL = re.compile(r"\b(?:patients?|clinical|trial|case|treatment|therapy|targeted|efficacy|"
                      r"diagnos\w*|response|survival|mutated)\b", re.IGNORECASE)
NEGATIVE = re.compile(r"\b(?:review|protocol|radiomics|radiogenomics|machine learning|bioinformatic|"
                      r"in vitro|mice|murine|xenograft|preclinical|systematic|meta-analysis|imaging|"
                      r"cohort profile)\b", re.IGNORECASE)
LICENSE = re.compile(r"https?://creativecommons\.org/(?:licenses/by/4\.0|publicdomain/zero/1\.0)/?", re.IGNORECASE)
VARIANT = re.compile(r"(?<![A-Za-z0-9])(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}"
                     r"[ACDEFGHIKLMNPQRSTVWY*](?![A-Za-z0-9])", re.IGNORECASE)
ADDITIONAL_EXCLUDED = {"42719457", "42756523", "42840361", "42864811", "42817873", "42784734",
                       "42872089", "42872145", "34096690", "40445863", "42584173"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def order(value):
    return hashlib.sha256(f"{SEED}|{value}".encode()).hexdigest()


def excluded_pmids():
    return ADDITIONAL_EXCLUDED | {str(row["pmid"]) for name in ("development.json", "holdout.json")
                                  for row in read(PRIOR / name)}


def candidate_rank(record):
    """Rank existing extracted candidates, never by their target pass/fail."""
    fact = record.get("fact") or {}
    text = json.dumps({k: fact.get(k) for k in ("population", "conditions", "treatment", "results")},
                      ensure_ascii=False)
    entities = record.get("entities") or []
    kinds = {row.get("kind") for row in entities if isinstance(row, dict)}
    score = (4 * (fact.get("relation") == "treatment_outcome")
             + 3 * bool(VARIANT.search(text)) + 2 * ("drug" in kinds)
             + 2 * ("variant" in kinds) + bool(fact.get("results")))
    return -score, order(record["group_id"])


def source_pool(root, rows):
    by_batch = defaultdict(set)
    excluded = excluded_pmids()
    for row in rows:
        if row.get("pmid") and row["pmid"] not in excluded:
            by_batch[row["batch_id"]].add(row["pmid"])
    articles = {}
    for batch_id, pmids in by_batch.items():
        snapshot = read(root / "batches" / batch_id / "sources/snapshot.json")
        if snapshot.get("sha256") != source_digest(snapshot.get("snapshot")):
            raise ValueError(f"Snapshot checksum differs: {batch_id}")
        for doc in snapshot["snapshot"]["documents"]:
            pmid = str(doc.get("pmid") or "")
            if pmid not in pmids or pmid in articles:
                continue
            version_key = str(doc.get("fulltext_version_id") or "").replace("-", "")
            versions = [v for v in doc.get("versions", [])
                        if str(v.get("id") or "").replace("-", "") == version_key]
            if len(versions) != 1 or not LICENSE.search(str(versions[0].get("rights") or "")):
                continue
            title = doc.get("title") or ""
            articles[pmid] = {"pmid": pmid, "title": title, "batch_id": batch_id,
                              "signal": bool(SIGNAL.search(title) and CLINICAL.search(title)),
                              "challenge": bool(NEGATIVE.search(title))}
    return articles


def freeze(root, output):
    root, output = Path(root), Path(output)
    if output.exists():
        raise FileExistsError("A frozen comparison must not overwrite an earlier one")
    rows = queue_rows(root)
    pool = source_pool(root, rows)
    signal = sorted((a for a in pool.values() if a["signal"] and not a["challenge"]),
                    key=lambda a: order(a["pmid"]))[:10]
    chosen = {a["pmid"] for a in signal}
    challenge = sorted((a for a in pool.values() if a["challenge"] and a["pmid"] not in chosen),
                       key=lambda a: order(a["pmid"]))[:10]
    if len(signal) != 10 or len(challenge) != 10:
        raise ValueError(f"Insufficient independent licensed source pool: {len(signal)} signal, {len(challenge)} challenge")
    selected = [{**row, "sampling_group": group} for group, values in
                (("source_signal", signal), ("source_challenge", challenge)) for row in values]
    by_article = defaultdict(list)
    for row in rows:
        by_article[(row["batch_id"], row["pmid"])].append(row)
    by_batch, chosen_rows = {}, []
    for article in selected:
        batch, pmid = article["batch_id"], article["pmid"]
        if batch not in by_batch:
            records, _, _, _ = verified_batch(root, {"id": batch})
            by_batch[batch] = {row["group_id"]: row for row in records}
        possible = [row for row in by_article[(batch, pmid)] if row["group_id"] in by_batch[batch]]
        possible.sort(key=lambda row: candidate_rank(by_batch[batch][row["group_id"]]))
        if not possible:
            raise ValueError(f"No extracted candidate found for {pmid}")
        chosen_rows.extend({**row, "sample_role": "holdout", "sampling_group": article["sampling_group"]}
                           for row in possible[:3])
    samples = materialize(root, chosen_rows)
    by_pmid = {}
    for sample in samples:
        by_pmid.setdefault(sample["pmid"], sample)
    source_articles = []
    for article in selected:
        sample = by_pmid[article["pmid"]]
        doc, record = sample["source_document"], sample["record"]
        if doc["passages_sha256"] != source_digest(doc["passages"]):
            raise ValueError(f"Source passage checksum differs: {article['pmid']}")
        source_articles.append({"pmid": article["pmid"], "title": article["title"],
             "sampling_group": article["sampling_group"], "version_id": record["version_id"],
             "source_sha256": record["source_sha256"], "rights": record["article"].get("rights"),
             "passages": [{"id": p["id"], "locator": p["locator"], "text": p["text"]}
                          for p in doc["passages"] if p.get("text")]})
    source = {"version": "target-holdout-source-only-v1", "articles": source_articles,
              "candidate_outputs_included": False,
              "note": "Title signals are sampling strata, not reference labels; full source remains available."}
    manifest = {"version": "target-holdout-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "seed": SEED, "article_count": len(selected), "candidate_count": len(samples),
                "source_pool_count": len(pool), "strata": dict(Counter(a["sampling_group"] for a in selected)),
                "max_candidates_per_article": 3, "samples_sha256": digest(samples), "source_sha256": digest(source),
                "selection": "Source-title strata before comparison; up to three old candidates per article, ranked without target status.",
                "rights_filter": "Source explicitly declares CC BY 4.0 or CC0 1.0", "target_contacted": False,
                "target_written": False, "production_database_written": False}
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("selection.json", selected), ("samples.json", samples),
                        ("source-only.json", source), ("manifest.json", manifest)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    return {"articles": len(selected), "candidate_records": len(samples), "pool_articles": len(pool),
            "strata": manifest["strata"], "source_sha256": manifest["source_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.output), ensure_ascii=False))
