"""Evaluate optional entity deferral against frozen 012 outputs, without models."""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.core_knowledge_intake import assemble
from apps.curation.core_knowledge_name_resolution import resolve
from apps.curation.core_knowledge_optional_entities import (
    VERSION as OPTIONAL_VERSION,
)
from apps.curation.core_knowledge_optional_entities import defer_optional_entities
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
from scripts.resolve_core_knowledge_names import verified_alignment

BASE = Path("var/experiments/core-knowledge-20261029")
VERSION = "core-knowledge-gaps-evaluation-v1"
PREFIX = "core-gaps-20261029"


def check_deferral(before, adapted):
    expected = deepcopy(before)
    ids = expected["payload"]["fact"]["entity_ids"]
    refs = adapted["unresolved_entity_refs"]
    deferred = {ref["entity_id"] for ref in refs}
    require(deferred <= set(ids), "Deferral added an unknown reference")
    require(
        deferred == {change["entity_id"] for change in adapted["changes"]},
        "Unrecorded entity deferral",
    )
    expected["payload"]["fact"]["entity_ids"] = [
        eid for eid in ids if eid not in deferred
    ]
    require(adapted["result_view"] == expected, "Adapter changed more than entity IDs")


def build(root):
    """Read-only build; the CLI freezes packets and outputs after validation."""
    require(root.resolve() != BASE.resolve(), "Cannot write into the frozen 012 root")
    baseline_path = BASE / "name-resolution.json"
    baseline = read(baseline_path)
    for source in baseline["sources"]:
        require(sha(Path(source["path"])) == source["sha256"], "012 source changed")
    packets_doc = read(BASE / "packets.json")
    if (root / "packets.json").exists():
        require(read(root / "packets.json") == packets_doc, "013 inputs differ")
    packets = indexed(packets_doc["records"], "packets")
    prior = indexed(baseline["records"], "012 records")
    require(
        set(packets) == set(prior) == {f"K{i:02}" for i in range(1, 16)},
        "Need the same 15 groups",
    )
    rows, inputs, deferred_ids = [], [], set()
    packet_sha = sha(BASE / "packets.json")
    for rid, packet in sorted(packets.items()):
        path = BASE / "extract" / f"result-{rid}.json"
        raw, before = read(path), prior[rid]
        original = deepcopy(raw)
        digest = sha(path)
        require(
            raw["id"] == rid and digest == before["result_sha256"],
            f"Raw result changed: {rid}",
        )
        started = perf_counter()
        named = resolve(packet, raw)
        require(
            verified_alignment(packet, raw, named) == before["case_alignment_verified"],
            f"Name verification differs: {rid}",
        )
        require(
            named["changes"] == before["case_only_changes"]
            and named["unresolved"] == before["unresolved"],
            f"Name resolution differs: {rid}",
        )
        require(
            assemble(packet, named["result_view"]) == before["assembly"],
            f"012 assembly differs: {rid}",
        )
        adapted = defer_optional_entities(packet, named["result_view"])
        check_deferral(named["result_view"], adapted)
        assembly = assemble(packet, adapted["result_view"])
        refs, changes = adapted["unresolved_entity_refs"], adapted["changes"]
        deferred_ids.update((rid, ref["entity_id"]) for ref in refs)
        candidate = assembly["candidate"]
        if candidate:
            candidate["unresolved_entity_refs"] = deepcopy(refs)
            candidate["script_optional_entity_changes"] = deepcopy(changes)
            candidate["script_source"] = {
                "result_sha256": digest,
                "packets_sha256": packet_sha,
            }
        projection = project(candidate, packet) if candidate else None
        require(raw == original, f"Raw input mutated: {rid}")
        technical = assembly["structure_status"] == "core_structurally_valid"
        row = deepcopy(before)
        row.update(
            before_structure_status=before["assembly"]["structure_status"],
            before_ready_for_export=before["ready_for_export"],
            name_aligned_assembly=deepcopy(before["assembly"]),
            assembly=assembly,
            projection=projection,
            technical_ready_for_export=technical,
            ready_for_export=before["resolved_review"]["core_usable"] and technical,
            unresolved_entity_refs=deepcopy(refs),
            optional_entity_changes=deepcopy(changes),
            kept_entity_blockers=deepcopy(adapted["kept_blockers"]),
            optional_entity_version=adapted["version"],
            script_seconds=perf_counter() - started,
        )
        rows.append(row)
        inputs.append(path)
    require(
        deferred_ids == {("K07", "K07.e20")},
        "Unexpected deferral in fixed 15-group experiment",
    )
    items, merges = knowledge_items(rows, packets)
    by_id = indexed(rows, "013 records")
    for item in items:
        item["id"] = item["id"].replace("core-knowledge-20261029:", f"{PREFIX}:", 1)
        item["optional_entity_version"] = OPTIONAL_VERSION
        item["name_resolution_version"] = baseline["name_resolution_version"]
        item["review_scope"] = (
            "unchanged_012_review_with_script_optional_entity_deferral"
        )
        item["unresolved_entity_refs"] = []
        item["script_optional_entity_changes"] = []
        for source in item["source_groups"]:
            row = by_id[source["group_id"]]
            source.update(
                resolved_review=deepcopy(row["resolved_review"]),
                raw_assembled_candidate=deepcopy(row["original_assembly"]["candidate"]),
                name_aligned_candidate=deepcopy(
                    row["name_aligned_assembly"]["candidate"]
                ),
                case_only_changes=deepcopy(row["case_only_changes"]),
                unresolved_entity_refs=deepcopy(row["unresolved_entity_refs"]),
                optional_entity_changes=deepcopy(row["optional_entity_changes"]),
                assembly_scope="case_aligned_source_view_with_optional_entity_deferral",
            )
            item["unresolved_entity_refs"].extend(
                deepcopy(row["unresolved_entity_refs"])
            )
            item["script_optional_entity_changes"].extend(
                deepcopy(row["optional_entity_changes"])
            )
            require(
                source["reference_review"] == row["review"],
                "Original Astra review was replaced",
            )
            require(
                source["raw_model_fact"] == row["raw_model_fact"],
                "Original fact was replaced",
            )
            require(
                source["original_context"] == packets[row["id"]]["passages"],
                "Original English passages were changed",
            )
        item["notes"] += (
            " 013 changes only optional entity references. The original Astra and resolved 012 reviews are retained separately; no new semantic review or model call occurred."
        )
    for merge in merges:
        merge["into"] = merge["into"].replace(
            "core-knowledge-20261029:", f"{PREFIX}:", 1
        )
    paths = inputs + [
        baseline_path,
        BASE / "packets.json",
        BASE / "review.json",
        BASE / "evaluation.json",
        Path(__file__),
        Path("scripts/evaluate_core_knowledge.py"),
        Path("scripts/resolve_core_knowledge_names.py"),
    ]
    paths += [
        Path("apps/curation") / name
        for name in (
            "core_knowledge_intake.py",
            "core_knowledge_projection.py",
            "core_knowledge_name_resolution.py",
            "core_knowledge_optional_entities.py",
        )
    ]
    report = {
        "version": VERSION,
        "state": STATE,
        "denominator": len(rows),
        "optional_entity_version": OPTIONAL_VERSION,
        "projection_version": PROJECTION_VERSION,
        "name_resolution_version": baseline["name_resolution_version"],
        "scores": {
            "raw_core_usable": sum(r["raw_semantic_core_usable"] for r in rows),
            "core_usable": sum(r["resolved_review"]["core_usable"] for r in rows),
            "before_ready_for_export": sum(r["before_ready_for_export"] for r in rows),
            "structure_valid": sum(r["technical_ready_for_export"] for r in rows),
            "ready_for_export": sum(r["ready_for_export"] for r in rows),
            "exported_after_exact_merge": len(items),
            "deferred_entity_refs": len(deferred_ids),
            "raw_astra": scores([r["review"] for r in rows]),
            "pipeline": scores([r["resolved_review"] for r in rows]),
        },
        "timing": {
            "model_calls": 0,
            "script_seconds": sum(r["script_seconds"] for r in rows),
            "scope": "name alignment verification, optional entity deferral, assembly and projection; excludes I/O and export serialization",
        },
        "records": rows,
        "exact_merges": merges,
        "sources": [{"path": str(path), "sha256": sha(path)} for path in paths],
        "limits": [
            "Semantic reviews are unchanged from 012; this is a protocol adaptation, not improved model accuracy.",
            "Unlocated optional references are preserved as unresolved, not declared medically irrelevant.",
            "K12 remains blocked; preclinical evidence is not excluded as a class. Original English text and model statements remain unchanged.",
            "No new model inference, generated summary, clinical conclusion or production write is performed.",
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
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    destinations = [
        args.root / name
        for name in ("evaluation.json", "knowledge-items.json", "knowledge-items.jsonl")
    ]
    require(
        not any(path.exists() for path in destinations),
        "013 outputs already exist; refusing to overwrite",
    )
    report, exported = build(args.root)
    args.root.mkdir(parents=True, exist_ok=True)
    freeze(args.root / "packets.json", read(BASE / "packets.json"))
    freeze(destinations[0], report)
    exported["evaluation_sha256"] = sha(destinations[0])
    freeze(destinations[1], exported)
    freeze_jsonl(destinations[2], exported["records"])
    print(json.dumps(report["scores"], ensure_ascii=False))
