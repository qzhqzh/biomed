"""Validate a second source-only business-scope assessment before comparison."""

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
        raise ValueError("Source, review identity or independence differs")
    articles = {str(row["pmid"]): row for row in source["articles"]}
    records = reference.get("records")
    if (len(articles) != 20 or not isinstance(records, list) or len(records) != 20
            or {str(row.get("pmid")) for row in records} != set(articles)):
        raise ValueError("Reference must contain the 20 unique frozen articles")
    statuses = Counter()
    quote_count = event_count = 0
    for row in records:
        pmid = str(row["pmid"])
        article = articles[pmid]
        if row.get("title") != article.get("title"):
            raise ValueError(f"Source title differs for {pmid}")
        status = row.get("business_scope")
        if status not in {"in_scope_primary_human_tumor", "outside_primary_scope", "insufficient_evidence"}:
            raise ValueError(f"Invalid reference scope for {pmid}")
        statuses[status] += 1
        passages = {str(p["id"]): p for p in article["passages"]}
        if len(passages) != len(article["passages"]):
            raise ValueError(f"Duplicate source passage for {pmid}")
        events = row.get("events")
        if not isinstance(events, list):
            raise TypeError(f"Events must be an array for {pmid}")
        event_count += len(events)

        def check_quotes(items, passages=passages, pmid=pmid):
            nonlocal quote_count
            if not isinstance(items, list):
                raise TypeError(f"Evidence must be an array for {pmid}")
            for quote in items:
                passage = passages.get(str(quote.get("passage_id"))) if isinstance(quote, dict) else None
                text = quote.get("quote") if isinstance(quote, dict) else None
                if (not passage or quote.get("locator") != passage.get("locator") or not isinstance(text, str)
                        or not text or text not in passage.get("text", "")):
                    raise ValueError(f"Business reference quote does not match the frozen source: {pmid}")
                quote_count += 1

        check_quotes(row.get("article_evidence"))
        for event in events:
            if not isinstance(event, dict):
                raise TypeError(f"Invalid event for {pmid}")
            check_quotes(event.get("evidence"))
    result = {"version": "target-holdout-business-reference-integrity-v1", "articles": len(records),
              "statuses": dict(statuses), "events": event_count, "source_quotes_verified": quote_count,
              "source_sha256": manifest["source_sha256"], "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
              "reference_kind": "independent_astra_model_source_reference_not_expert_gold",
              "note": "Clinical relation scope does not establish target compatibility or treatment efficacy.",
              "created_at": datetime.now(timezone.utc).isoformat()}
    destination, validation = root / "astra-business-reference.json", root / "astra-business-reference-validation.json"
    if destination.exists() or validation.exists():
        raise FileExistsError("Frozen reference already exists")
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
