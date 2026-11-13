"""Validate and freeze an independent source-only model reference."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.knowledge_observation_report import digest


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def freeze(root, reference_path):
    root, reference_path = Path(root), Path(reference_path)
    manifest, source, reference = (read(root / "manifest.json"), read(root / "source-only.json"), read(reference_path))
    if (reference.get("model") != "gpt-6-astra" or source.get("candidate_outputs_included") is not False
            or reference.get("source_only_sha256") != manifest["source_sha256"]
            or digest(source) != manifest["source_sha256"]):
        raise ValueError("Reference identity, independence or source digest differs")
    articles = {str(row["pmid"]): row for row in source["articles"]}
    records = reference.get("records")
    if (len(articles) != 20 or not isinstance(records, list) or len(records) != 20
            or {str(r.get("pmid")) for r in records} != set(articles)):
        raise ValueError("Reference must contain the 20 unique frozen articles")
    statuses = Counter()
    quote_count = 0
    for row in records:
        pmid = str(row["pmid"])
        article = articles[pmid]
        if row.get("title") != article.get("title"):
            raise ValueError(f"Source title differs for {pmid}")
        status = row.get("conclusion_status")
        if status not in {"confirmed", "insufficient_evidence", "not_seen_in_examined_passages"}:
            raise ValueError(f"Invalid reference status for {pmid}")
        statuses[status] += 1
        passages = {str(p["id"]): p for p in article["passages"]}
        if len(passages) != len(article["passages"]):
            raise ValueError(f"Duplicate source passage for {pmid}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or (status == "confirmed" and not evidence):
            raise ValueError(f"Confirmed reference has no source quote: {pmid}")
        for quote in evidence:
            passage = passages.get(str(quote.get("passage_id"))) if isinstance(quote, dict) else None
            text = quote.get("quote") if isinstance(quote, dict) else None
            if (not passage or quote.get("locator") != passage.get("locator") or not isinstance(text, str)
                    or not text or text not in passage.get("text", "")):
                raise ValueError(f"Reference quote does not match the frozen source: {pmid}")
            quote_count += 1
    result = {"version": "target-holdout-source-reference-integrity-v1", "articles": len(records),
              "statuses": dict(statuses), "source_quotes_verified": quote_count,
              "source_sha256": manifest["source_sha256"], "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
              "reference_kind": "independent_astra_model_source_reference_not_expert_gold",
              "exact_quotes_do_not_prove_semantic_accuracy": True, "created_at": datetime.now(timezone.utc).isoformat()}
    destination, validation = root / "astra-source-reference.json", root / "astra-source-reference-validation.json"
    if destination.exists() or validation.exists():
        raise FileExistsError("Frozen source reference already exists")
    with destination.open("xb") as stream:
        stream.write(reference_path.read_bytes())
    with validation.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.reference), ensure_ascii=False))
