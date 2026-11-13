"""Entity-only ablation with original paragraph context retained by scripts."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.entity_link_guard import guard_link
from apps.curation.light_entity_linking import load_index, retrieve
from scripts.evaluate_knowledge_candidates import freeze, read, sha
from scripts.light_entity_pilot import (
    CHEMBL,
    EXTRACT,
    KINDS,
    ONCOTREE,
    STANDARDS,
    local_aliases,
    locate,
    passage_prompt,
    runner_row,
)

BASE = Path("var/experiments/light-entities-20261029")
PROMPT = (
    EXTRACT.split("\nmodifiers are short verbatim phrases", 1)[0]
    .replace("with two lists:", "with one list:")
    .replace(',"modifiers":[{"text":"EXACT relevant qualifier from a passage"}]', "")
    + "\nDeduplicate repeated mentions with the same type/text. Return valid JSON only, with the entities list and no markdown."
)


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    data = read(BASE / "source-inputs.json")
    freeze(root / "source-inputs.json", data)
    freeze(root / "reference.json", read(BASE / "reference.json"))
    freeze(
        root / "definition.json",
        {"version": "lean-entity-only-v1", "system_prompt": PROMPT},
    )
    freeze(
        root / "inputs.json",
        {"records": [runner_row(p["id"], passage_prompt(p)) for p in data["records"]]},
    )
    freeze(
        root / "source-context.json",
        {
            "version": "original-context-v1",
            "generated": False,
            "note": "Full frozen paragraphs are source context, not extracted or verified conditions.",
            "records": [
                {"id": p["id"], "article": p["article"], "passages": p["passages"]}
                for p in data["records"]
            ],
        },
    )
    freeze(
        root / "plan.json",
        {
            "version": "lean-entity-ablation-v1",
            "sample_count": 15,
            "extract_change": "Remove model-generated modifiers only; same entity instructions, input paragraphs, 9B revision, runtime, GPU and 2048 output limit as 009.",
            "reference": "Reuse frozen 009 assistant development reference, not new Astra gold or human gold.",
            "link_change": "Evaluate generic conservative guards on existing 009 links separately from extraction; no test-identity denylist.",
            "workflow_goal": "One entity extraction call plus script context/lookup/guards; retain extra model calls only if measured benefit justifies cost.",
            "metrics": [
                "complete valid core sets /15",
                "missing items with explicit denominator",
                "fixed 009 required-set paired diagnostic",
                "type problems",
                "wrong and retained correct links",
                "unresolved",
                "tokens, calls and total time",
            ],
            "limits": [
                "No training, model download, production write, tumor_api change or collection setting change",
                "Whole source context retention is not condition extraction accuracy",
                "No measured human time savings",
                "Original incomplete JSON remains a failure; no hidden repair",
            ],
            "sources": [
                {"path": str(BASE / name), "sha256": sha(BASE / name)}
                for name in (
                    "source-inputs.json",
                    "reference.json",
                    "definition.json",
                    "evaluation.json",
                    "link-review.json",
                )
            ],
        },
    )
    print(f"Prepared {len(data['records'])} immutable entity-only inputs")


def preferred_candidate(found, kind):
    """Same preferred-label selection as 009; no alias auto-confirmation."""
    if found["truncated"]:
        return None
    order = {
        "drug": ("RXNORM", "CHEMBL"),
        "disease": ("MONDO", "ONCOTREE"),
    }.get(kind, ("NCBIGene",))
    for namespace in order:
        candidates = [c for c in found["candidates"] if c["namespace"] == namespace]
        if len(candidates) > 1:
            break
        direct = [c for c in candidates if any(
            m["dictionary_field"] == "label" and m["query_source"] == "mention"
            for m in c["matching_evidence"]
        )]
        if len(direct) == 1:
            return direct[0]
    return None


def assemble(root):
    start = time.perf_counter()
    metadata = read(root / "guard-metadata.json")
    index = load_index(STANDARDS, oncotree=ONCOTREE, chembl=CHEMBL)
    load_seconds = time.perf_counter() - start
    records = []
    for packet in read(root / "source-inputs.json")["records"]:
        start = time.perf_counter()
        rid = packet["id"]
        result = read(root / "extract" / f"result-{rid}.json")
        payload = result.get("payload")
        valid = (isinstance(payload, dict) and set(payload) == {"entities"}
                 and isinstance(payload["entities"], list))
        rows = payload["entities"] if valid else []
        entries, seen = [], {}
        for pos, row in enumerate(rows):
            eid = f"{rid}.e{pos + 1}"
            row_valid = (isinstance(row, dict) and set(row) == {"type", "text"}
                         and isinstance(row.get("type"), str) and row["type"] in KINDS
                         and isinstance(row.get("text"), str) and bool(row["text"]))
            if not row_valid:
                valid = False
                entries.append({"id": eid, "raw": row, "raw_indices": [pos],
                                "status": "invalid_entity", "locations": [], "link": None})
                continue
            mention, kind = row["text"], row["type"]
            key = (kind, mention)
            if key in seen:
                seen[key]["raw_indices"].append(pos)
                continue
            locations = locate(packet, mention)
            aliases = next((
                [alias] for alias in sorted(local_aliases(packet, mention), key=len, reverse=True)
                if locations and retrieve(index, kind, alias)["total_candidates"]
            ), [])
            found = retrieve(index, kind, mention, aliases=aliases)
            guards = {c["id"]: guard_link(row, c, packet, metadata)
                      for c in found["candidates"]}
            proposed = preferred_candidate(found, kind) if locations else None
            selected = proposed and guards[proposed["id"]]["decision"] == "retain"
            candidate_id = proposed["id"] if selected else None
            entry = {
                "id": eid, **row, "raw_indices": [pos],
                "status": "located" if locations else "unlocated", "locations": locations,
                "aliases": aliases, "candidates": found, "candidate_guards": guards,
                "link": {"candidate_id": candidate_id,
                         "proposed_id": proposed["id"] if proposed else None,
                         "method": "unique_label_guarded" if selected else "unresolved",
                         "status": "research_candidate_not_confirmed"},
                "local_id": None if selected else f"local:lean-entity-only-v1:{eid}",
            }
            entries.append(entry)
            seen[key] = entry
        records.append({
            "id": rid, "article": packet["article"],
            "source_context": {"artifact": "source-context.json", "record_id": rid,
                               "generated": False, "conditions_verified": False},
            "raw_json_valid": isinstance(payload, dict), "schema_valid": valid,
            "raw_entity_count": len(rows), "entities": entries,
            "script_seconds": time.perf_counter() - start,
            "inference_seconds": result["seconds"],
        })
    freeze(root / "assembled.json", {
        "version": "lean-one-call-workflow-v1", "extra_model_calls": 0,
        "index_load_seconds": load_seconds, "records": records,
        "source_hashes": {str(p): sha(p) for p in (
            root / "guard-metadata.json", root / "source-context.json",
            STANDARDS, ONCOTREE, CHEMBL, Path(__file__),
            Path("apps/curation/entity_link_guard.py"),
            Path("apps/curation/light_entity_linking.py"),
        )},
    })
    print(f"Assembled {len(records)} groups; no additional model calls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--assemble", action="store_true")
    args = parser.parse_args()
    (assemble if args.assemble else prepare)(args.root)


if __name__ == "__main__":
    main()
