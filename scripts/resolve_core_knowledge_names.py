"""Freeze a separate source-case alignment report without altering raw reviews."""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.core_knowledge_intake import _drug_locations, assemble
from apps.curation.core_knowledge_name_resolution import resolve
from apps.curation.core_knowledge_projection import VERSION as PROJECTION_VERSION
from apps.curation.core_knowledge_projection import project
from scripts.evaluate_core_knowledge import (
    STATE,
    freeze_jsonl,
    indexed,
    knowledge_items,
    require,
    scores,
)
from scripts.evaluate_knowledge_candidates import freeze, read, sha

VERSION = "core-knowledge-name-resolution-evaluation-v1"
CASE_SCOPE = "script_case_alignment_of_source_supported_fact"


def verified_alignment(packet, raw, resolution):
    """Verify the resolver changed only names, retaining every original field."""
    view = resolution["result_view"]
    expected = deepcopy(raw)
    old_fact = (raw.get("payload") or {}).get("fact")
    new_fact = (view.get("payload") or {}).get("fact")
    if not isinstance(old_fact, dict) or not isinstance(new_fact, dict):
        require(view == raw and not resolution["changes"], "Unexpected non-fact edit")
        return False
    old_names, new_names = old_fact.get("drug_names"), new_fact.get("drug_names")
    expected["payload"]["fact"]["drug_names"] = deepcopy(new_names)
    require(view == expected, "Resolver edited fields beyond drug_names")
    require(isinstance(old_names, list) and isinstance(new_names, list), "Bad names")
    require(len(old_names) == len(new_names), "Resolver added or removed names")
    changes = indexed(
        [{**c, "id": c["index"]} for c in resolution["changes"]], "name changes"
    )
    altered = {
        i for i, names in enumerate(zip(old_names, new_names)) if names[0] != names[1]
    }
    require(set(changes) == altered, "Unrecorded or spurious case change")
    for index in altered:
        before, after, change = old_names[index], new_names[index], changes[index]
        require(
            isinstance(before, str)
            and isinstance(after, str)
            and before.casefold() == after.casefold()
            and change["method"] == "case_only_source_alignment"
            and change["model_name"] == before
            and change["source_name"] == after
            and bool(_drug_locations(packet, after)),
            "Unsupported name alignment",
        )
    return (
        bool(altered)
        and not resolution["unresolved"]
        and all(
            isinstance(name, str) and bool(_drug_locations(packet, name))
            for name in new_names
        )
    )


def pipeline_review(original, aligned):
    """Lift only a resolved case-only blocker, never a clinical blocker."""
    review = deepcopy(original)
    review["scope"] = "original_astra_review_unchanged"
    review["additional_model_calls"] = 0
    blockers = original["blocking"]
    if (
        aligned
        and original["clinical_core_supported"]
        and blockers
        and all(b["category"] == "drug_literal_case_change" for b in blockers)
    ):
        review.update(
            scope=CASE_SCOPE,
            core_usable=True,
            literal_drugs=True,
            drug_fidelity_status="script_case_aligned_to_literal_source",
            blocking=[],
            resolved_blocking=deepcopy(blockers),
            resolution_note="Only source spelling was aligned by script; Astra was not called again.",
        )
    return review


def build(root):
    original = read(root / "evaluation.json")
    for source in original["sources"]:
        require(
            sha(Path(source["path"])) == source["sha256"], "Original source changed"
        )
    packets = indexed(read(root / "packets.json")["records"], "packets")
    prior = indexed(original["records"], "original evaluation")
    reviews = indexed(read(root / "review.json")["records"], "original reviews")
    require(
        set(packets)
        == set(prior)
        == set(reviews)
        == {f"K{i:02}" for i in range(1, 16)},
        "Need same 15 groups",
    )
    rows, inputs = [], []
    for rid, packet in sorted(packets.items()):
        path = root / "extract" / f"result-{rid}.json"
        raw, before = read(path), prior[rid]
        require(
            raw["id"] == rid
            and sha(path) == before["result_sha256"] == reviews[rid]["result_sha256"],
            f"Raw result changed: {rid}",
        )
        require(before["review"] == reviews[rid], f"Original review changed: {rid}")
        require(
            assemble(packet, raw) == before["assembly"],
            f"Original assembly changed: {rid}",
        )
        started = perf_counter()
        resolution = resolve(packet, raw)
        aligned = verified_alignment(packet, raw, resolution)
        assembly = assemble(packet, resolution["result_view"])
        projection = (
            project(assembly["candidate"], packet) if assembly["candidate"] else None
        )
        effective = pipeline_review(reviews[rid], aligned)
        technical_ready = assembly["structure_status"] == "core_structurally_valid"
        elapsed = perf_counter() - started
        rows.append(
            {
                "id": rid,
                "result_sha256": sha(path),
                "raw_model_fact": deepcopy((raw.get("payload") or {}).get("fact")),
                "old_review": deepcopy(before["old_review"]),
                "review": deepcopy(reviews[rid]),
                "resolved_review": effective,
                "raw_semantic_core_usable": reviews[rid]["core_usable"],
                "pipeline_core_usable": effective["core_usable"],
                "technical_ready_for_export": technical_ready,
                "ready_for_export": effective["core_usable"] and technical_ready,
                "before_structure_status": before["assembly"]["structure_status"],
                "before_ready_for_export": before["ready_for_export"],
                "original_assembly": deepcopy(before["assembly"]),
                "assembly": assembly,
                "projection_version": PROJECTION_VERSION,
                "projection": projection,
                "name_resolution_version": resolution["version"],
                "case_only_changes": resolution["changes"],
                "unresolved": resolution["unresolved"],
                "case_alignment_verified": aligned,
                "script_seconds": elapsed,
            }
        )
        inputs.append(path)
    items, merges = knowledge_items(rows, packets)
    by_id = indexed(rows, "resolved records")
    for item in items:
        item["review_scope"] = "raw_astra_review_with_separate_script_name_alignment"
        item["name_resolution_version"] = rows[0]["name_resolution_version"]
        item["notes"] += (
            " reference_review remains the raw Astra review; resolved_review is a separate script decision, not another model review."
        )
        for source in item["source_groups"]:
            row = by_id[source["group_id"]]
            source["resolved_review"] = deepcopy(row["resolved_review"])
            source["raw_assembled_candidate"] = deepcopy(
                row["original_assembly"]["candidate"]
            )
            source["assembly_scope"] = "case_aligned_source_view"
            source["case_only_changes"] = deepcopy(row["case_only_changes"])
    paths = inputs + [
        root / name
        for name in (
            "evaluation.json",
            "packets.json",
            "review.json",
            "reference.json",
            "assembled.json",
        )
    ]
    paths += [Path(__file__), Path("scripts/evaluate_core_knowledge.py")] + [
        Path("apps/curation") / name
        for name in (
            "core_knowledge_intake.py",
            "core_knowledge_projection.py",
            "core_knowledge_name_resolution.py",
        )
    ]
    report = {
        "version": VERSION,
        "name_resolution_version": rows[0]["name_resolution_version"],
        "projection_version": PROJECTION_VERSION,
        "state": STATE,
        "denominator": len(rows),
        "scores": {
            "raw_core_usable": sum(r["raw_semantic_core_usable"] for r in rows),
            "core_usable": sum(r["pipeline_core_usable"] for r in rows),
            "structure_valid": sum(r["technical_ready_for_export"] for r in rows),
            "ready_for_export": sum(r["ready_for_export"] for r in rows),
            "exported_after_exact_merge": len(items),
            "case_aligned_groups": sum(bool(r["case_only_changes"]) for r in rows),
            "case_only_blockers_resolved": sum(
                r["resolved_review"]["scope"] == CASE_SCOPE for r in rows
            ),
            "raw_astra": scores([r["review"] for r in rows]),
            "pipeline": scores([r["resolved_review"] for r in rows]),
        },
        "timing": {
            "model_calls": 0,
            "script_seconds": sum(r["script_seconds"] for r in rows),
        },
        "records": rows,
        "exact_merges": merges,
        "sources": [{"path": str(path), "sha256": sha(path)} for path in paths],
        "limits": [
            "Case alignment is a script operation, not a new Astra review or improved model accuracy.",
            "No new drugs, entity IDs, aliases or relationships are inferred. Other structural and semantic blockers remain.",
            "Raw artifacts are unchanged. Research export is not production publication.",
        ],
    }
    return report, {
        "version": VERSION,
        "state": STATE,
        "records": items,
        "exact_merges": merges,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    destinations = [
        args.root / name
        for name in (
            "name-resolution.json",
            "knowledge-items-resolved.json",
            "knowledge-items-resolved.jsonl",
        )
    ]
    require(
        not any(path.exists() for path in destinations),
        "Resolved artifacts already exist; refusing to overwrite",
    )
    report, exported = build(args.root)
    freeze(destinations[0], report)
    exported["name_resolution_sha256"] = sha(destinations[0])
    freeze(destinations[1], exported)
    freeze_jsonl(destinations[2], exported["records"])
    print(json.dumps(report["scores"], ensure_ascii=False))
