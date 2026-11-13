"""Freeze source-only articles and separate old candidates for a blind review."""

import json
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.decision import digest as source_digest
from apps.curation.source_linked_facts import evidence_index
from scripts.freeze_effective_knowledge_sample import materialize
from scripts.knowledge_observation_report import DEFAULT_ROOT, digest

ROOT = Path("var/experiments/knowledge-target-optimization-20261112-v1")
YIELD = Path("var/experiments/target-yield-20261106")
PMIDS = ("42719457", "42756523", "42840361", "42864811", "42817873", "42784734", "42872089", "42872145")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def freeze(output):
    available = (read(ROOT / "positive-probe-v1/retrieved-context-v2/samples.json")
                 + read(ROOT / "development.json") + read(ROOT / "context-probe-v1/samples.json"))
    other = [{"batch_id": "kp-20261109T211449-4ef5b485", "pmid": "42784734",
              "group_id": "E-42784734-0068.f02", "sample_role": "identity_probe",
              "status": "diagnostic_only", "reason_code": "gene_variant_disambiguation"}]
    available += materialize(DEFAULT_ROOT, other)
    by_pmid = {str(row["pmid"]): row for row in available if str(row["pmid"]) in PMIDS}
    for source in ("42719457", "42756523", "42840361", "42864811", "42817873", "42784734"):
        row = by_pmid[source]
        if digest({k: v for k, v in row.items() if k != "sample_sha256"}) != row["sample_sha256"]:
            raise ValueError(f"Frozen candidate changed: {source}")
        if row["record"].get("anchors") and any(a.get("purpose") == "evidence" for a in row["record"]["anchors"]):
            evidence_index(row["record"], row["source_document"])
        elif row["source_document"]["passages_sha256"] != source_digest(row["source_document"]["passages"]):
            raise ValueError(f"Source passages changed: {source}")
    candidates, articles = [], []
    for pmid in PMIDS:
        if pmid in {"42872089", "42872145"}:
            source = read(YIELD / "sources" / f"{pmid}.json")
            article = source["article"]
            if source["verified_sha256"] != article["source_sha256"]:
                raise ValueError(f"Historical article digest differs: {pmid}")
            passages = source["passages"]
        else:
            sample = by_pmid[pmid]
            candidates.append(sample)
            article, passages = sample["record"]["article"], sample["source_document"]["passages"]
        version = str(article["version_id"]).replace("-", "")
        if not passages or any(str(p.get("version_id", "")).replace("-", "") != version for p in passages):
            raise ValueError(f"Passage version differs: {pmid}")
        articles.append({"pmid": pmid, "title": article.get("title"), "version_id": article["version_id"],
                         "source_sha256": article["source_sha256"], "rights": article.get("rights"),
                         "passages": [{"id": p["id"], "locator": p["locator"], "text": p["text"]} for p in passages]})
    source_only = {"version": "target-identity-source-only-v1", "articles": articles,
                   "selection": "Eight intentionally selected articles; not representative; no old predictions included."}
    manifest = {"version": "target-identity-sample-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "pmids": list(PMIDS), "source_only_sha256": digest(source_only), "candidates_sha256": digest(candidates),
                "selection_note": "Selected diagnostic positives, negative contexts and historical controls; not a random sample.",
                "target_written": False}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in (("source-only.json", source_only), ("old-candidates.json", candidates), ("manifest.json", manifest)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    return {"articles": len(articles), "old_candidates": len(candidates), "source_only_sha256": manifest["source_only_sha256"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(freeze(parser.parse_args().output), ensure_ascii=False))
