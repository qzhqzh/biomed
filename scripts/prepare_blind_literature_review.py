"""Make an article-source-only review packet, independent of candidate outputs.

The selected excerpts are a browsing aid, not proof that the full article was
exhaustively reviewed. No model, candidate fact or prior target decision is used.
"""

import argparse
import json
import re
from pathlib import Path

from scripts.knowledge_observation_report import digest as snapshot_digest

EVENT = re.compile(r"\b(?:mutation|mutant|mutated|variant|amplification|rearrangement|fusion|cnv)\b|"
                   r"(?<!\w)(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*](?!\w)",
                   re.IGNORECASE)
CLINICAL = re.compile(r"\b(?:patients?|treated|treatment|therapy|response|responded|survival|"
                      r"diagnos\w*|prognos\w*|outcome)\b", re.IGNORECASE)


def packet(samples):
    output = []
    for sample in samples:
        if snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample.get("sample_sha256"):
            raise ValueError("Frozen sample hash differs")
        passages = sample["source_document"]["passages"]
        abstract = [p for p in passages if "/abstract/" in str(p.get("locator", "")) and p.get("text")]
        body = []
        for p in passages:
            text, locator = p.get("text", ""), str(p.get("locator", ""))
            if not locator.startswith("body/") or not text:
                continue
            score = bool(EVENT.search(text)) + bool(CLINICAL.search(text))
            if score == 2:
                body.append(p)
        selected = abstract + body[:3]
        output.append({"pmid": sample["pmid"], "title": sample["record"].get("article", {}).get("title"),
                       "source_sha256": sample["record"]["source_sha256"],
                       "source_passage_count": len(passages),
                       "passages": [{"id": p["id"], "locator": p["locator"], "text": p["text"]} for p in selected]})
    return {"version": "biomed.blind-article-review-packet.v1", "articles": output,
            "selection_note": "Full abstract plus up to three body passages selected by broad event and clinical terms; full article not exhaustively reviewed.",
            "candidate_outputs_included": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = packet(json.loads(args.samples.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"articles": len(result["articles"]), "candidate_outputs_included": False}))


if __name__ == "__main__":
    main()
