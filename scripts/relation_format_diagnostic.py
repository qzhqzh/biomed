"""Post-hoc lossless shape adaptation; never rewrite relation semantics."""

import argparse
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_knowledge_candidates import freeze, read, sha
from scripts.relation_fact_pilot import assemble_one


def adapt(payload):
    result, changes = deepcopy(payload), []
    if not isinstance(result, dict) or not isinstance(result.get("fact"), dict):
        return result, changes
    if set(result) == {"fact"}:
        result["reason"] = None
        changes.append("add_absent_abstention_reason_null_when_fact_present")
    condition = result["fact"].get("conditions")
    if isinstance(condition, str) and condition.strip():
        result["fact"]["conditions"] = [condition]
        changes.append("wrap_original_condition_string_as_single_list_item")
    return result, changes


def run(root):
    freeze(
        root / "format-diagnostic-plan.json",
        {
            "version": "relation-lossless-shape-ablation-v1",
            "timing": "Post-hoc diagnosis after observing raw structural failures; primary score and raw predictions unchanged.",
            "changes": [
                "For root with only a non-null fact, add reason:null",
                "Wrap a nonempty conditions string in a one-item list without changing text",
            ],
            "excluded": [
                "No fact edits",
                "No condition splitting or inference",
                "No entity/role/value/source corrections",
                "No added ANCHOR citation",
                "No model calls",
            ],
            "evaluation": "Reuse raw Astra semantic review only because all semantic text and IDs remain identical; require adapted structural pass for usable.",
        },
    )
    packets = read(root / "packets.json")["records"]
    reviews = {
        x["id"]: x
        for part in ("a", "b")
        for x in read(root / f"astra-review-{part}.json")["records"]
    }
    records = []
    for packet in packets:
        start = time.perf_counter()
        path = root / "extract" / f"result-{packet['id']}.json"
        raw = read(path)
        payload, changes = adapt(raw["payload"])
        raw_assembly = assemble_one(packet, raw)
        adapted_assembly = assemble_one(packet, {**raw, "payload": payload})
        review = reviews[packet["id"]]
        assert review["result_sha256"] == sha(path)
        records.append(
            {
                "id": packet["id"],
                "raw_result_sha256": sha(path),
                "changes": changes,
                "raw_structure": raw_assembly["status"],
                "adapted_structure": adapted_assembly["status"],
                "semantic_usable": review["semantic_usable"],
                "adapted_usable": adapted_assembly["status"]
                == "structurally_valid_candidate"
                and review["semantic_usable"],
                "adapted_payload": payload,
                "adapted_assembly": adapted_assembly,
                "script_seconds": time.perf_counter() - start,
            }
        )
    artifact = {
        "version": "relation-lossless-shape-ablation-v1",
        "model_calls": 0,
        "raw_outputs_unchanged": True,
        "denominator": 15,
        "adapted_groups": sum(bool(x["changes"]) for x in records),
        "structurally_valid": sum(
            x["adapted_structure"] == "structurally_valid_candidate" for x in records
        ),
        "usable": sum(x["adapted_usable"] for x in records),
        "script_seconds": sum(x["script_seconds"] for x in records),
        "records": records,
        "source_hashes": {
            str(p): sha(p)
            for p in (
                root / "evaluation.json",
                root / "format-diagnostic-plan.json",
                Path(__file__),
            )
        },
    }
    freeze(root / "format-diagnostic.json", artifact)
    print(
        {
            k: artifact[k]
            for k in (
                "adapted_groups",
                "structurally_valid",
                "usable",
                "script_seconds",
            )
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    run(parser.parse_args().root)
