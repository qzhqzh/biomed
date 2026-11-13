"""Prepare a small, isolated Bonsai probe using only frozen development sources."""

import argparse
import json
from pathlib import Path

from apps.curation.decision import digest
from apps.curation.structured_knowledge_prompt import DEFINITION
from scripts.knowledge_observation_report import digest as snapshot_digest
from scripts.light_entity_pilot import runner_row

GROUPS = (
    "E-42756523-0009",  # source-supported case with deferred old mapping
    "E-42836110-0002",  # imaging fusion, not a gene fusion
    "E-42838582-0012",  # trial protocol citing prior study
    "E-42840361-0073",  # review and preclinical context
    "E-42852590-0036",  # mutually exclusive markers
    "E-42860291-0005",  # existing offline diagnostic control
)


def prepare(samples, output, *, groups=GROUPS):
    by_group = {row["group_id"]: row for row in samples}
    if len(by_group) != len(samples) or len(groups) != len(set(groups)):
        raise ValueError("Group IDs must be unique")
    if any(group not in by_group for group in groups):
        raise ValueError("The frozen development sample does not contain the fixed pilot cases")
    rows, lineage, skipped = [], [], []
    for group in groups:
        sample = by_group[group]
        if snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample.get("sample_sha256"):
            raise ValueError(f"Frozen sample hash differs: {group}")
        record = sample["record"]
        passages = []
        for anchor in record.get("anchors", []):
            if anchor.get("purpose") != "evidence":
                continue
            alias = (anchor.get("attributes") or {}).get("alias")
            if not alias or not anchor.get("quote") or not anchor.get("passage_id"):
                raise ValueError(f"Evidence lacks a source alias or passage: {group}")
            passages.append({"id": alias, "text": anchor["quote"]})
        if len({p["id"] for p in passages}) != len(passages):
            raise ValueError(f"Evidence passage aliases are ambiguous: {group}")
        if not passages:
            skipped.append({"group_id": group, "pmid": sample["pmid"], "sample_sha256": sample["sample_sha256"],
                            "reason": "old_extraction_has_no_evidence_passage"})
            continue
        inventory = [{"ref": e["ref"], "type": e["kind"], "text": e["label"]}
                     for e in record.get("entities", [])]
        prompt = json.dumps({"article_title": record.get("article", {}).get("title"),
                             "passages": passages, "entity_inventory": inventory}, ensure_ascii=False)
        rows.append(runner_row(group, prompt))
        lineage.append({"group_id": group, "pmid": sample["pmid"], "sample_sha256": sample["sample_sha256"],
                        "passage_ids": [p["id"] for p in passages]})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("inputs.json", {"records": rows}), ("definition.json", DEFINITION),
                        ("lineage.json", {"version": DEFINITION["version"], "records": lineage, "skipped": skipped,
                                          "inputs_sha256": digest({"records": rows})})):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    return {"output": str(output), "records": len(rows), "skipped": len(skipped), "version": DEFINITION["version"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--all", action="store_true", help="Use every frozen sample, without selecting by outcome")
    args = parser.parse_args()
    samples = json.loads(args.samples.read_text(encoding="utf-8"))
    groups = [row["group_id"] for row in samples] if args.all else GROUPS
    print(json.dumps(prepare(samples, args.output, groups=groups)))


if __name__ == "__main__":
    main()
