"""Validate and freeze the independent assessment of baseline candidates."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.knowledge_observation_report import digest


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def freeze(root, review_path):
    root, review_path = Path(root), Path(review_path)
    manifest, source, packet, review = (read(root / "manifest.json"), read(root / "source-only.json"),
                                      read(root / "baseline-review-packet.json"), read(review_path))
    if (digest(source) != manifest["source_sha256"] or packet["source_sha256"] != manifest["source_sha256"]
            or review.get("model") != "gpt-6-astra" or packet.get("new_model_outputs_included") is not False
            or review.get("baseline_packet_sha256") != hashlib.sha256((root / "baseline-review-packet.json").read_bytes()).hexdigest()):
        raise ValueError("Source, review identity or baseline packet differs")
    articles = {str(row["pmid"]): row for row in source["articles"]}
    expected = {row["group_id"]: row for row in packet["records"]}
    records = review.get("records")
    if (len(expected) != 30 or not isinstance(records, list) or len(records) != 30
            or {row.get("group_id") for row in records} != set(expected)):
        raise ValueError("Review must cover all 30 unique baseline candidates")
    counts, quotes = Counter(), 0
    for row in records:
        original = expected[row["group_id"]]
        pmid = str(row.get("pmid"))
        if pmid != original["pmid"] or pmid not in articles:
            raise ValueError("Review article and candidate differ")
        judgment = row.get("judgment")
        if judgment not in {"supported", "unsupported", "uncertain", "partial", "no_fact"}:
            raise ValueError(f"Unknown review judgment: {judgment}")
        counts[judgment] += 1
        passages = {str(p["id"]): p for p in articles[pmid]["passages"]}
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"Review must cite its source for {row['group_id']}")
        for quote in evidence:
            passage = passages.get(str(quote.get("passage_id"))) if isinstance(quote, dict) else None
            text = quote.get("quote") if isinstance(quote, dict) else None
            if (not passage or quote.get("locator") != passage.get("locator") or not isinstance(text, str)
                    or not text or text not in passage.get("text", "")):
                raise ValueError(f"Baseline review quotation does not match source: {row['group_id']}")
            quotes += 1
    result = {"version": "target-holdout-baseline-assessment-integrity-v1", "records": len(records),
              "judgments": dict(counts), "source_quotes_verified": quotes,
              "packet_sha256": review["baseline_packet_sha256"],
              "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
              "reference_kind": "independent_astra_model_assessment_not_expert_gold",
              "note": "Source support is separate from independent target compatibility and full-library novelty.",
              "created_at": datetime.now(timezone.utc).isoformat()}
    destination, validation = root / "astra-baseline-review.json", root / "astra-baseline-review-validation.json"
    if destination.exists() or validation.exists():
        raise FileExistsError("Frozen baseline review already exists")
    with destination.open("xb") as stream:
        stream.write(review_path.read_bytes())
    with validation.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root, args.review), ensure_ascii=False))
