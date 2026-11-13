"""Reproduce retrospective diagnostics; never repair or publish knowledge."""

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.compact_facts import assemble, build_units
from scripts.evaluate_knowledge_candidates import freeze, indexes, key, read, sha

AXES = ("entity", "relation", "condition", "numerical")
STATES = {"issue", "no_observed_issue", "not_assessable", "not_applicable"}
CATEGORIES = {
    "entity_identity",
    "entity_grounding",
    "relation_binding",
    "condition_scope",
    "numerical_binding",
    "protocol",
    "evidence_grounding",
}


def build(source, standards, audit_files):
    packets = {r["id"]: r for r in read(source / "source-inputs.json")["records"]}
    units = {r["id"]: r["units"] for r in read(source / "units.json")["records"]}
    rows = [r for path in audit_files for r in read(path)["records"]]
    if len(rows) != 15 or {r["id"] for r in rows} != set(packets):
        raise ValueError("Audit must cover the same 15 groups exactly once")
    index = indexes(read(standards))
    axis_counts = {axis: Counter() for axis in AXES}
    categories = Counter()
    entity_counts, link_counts = Counter(), Counter()
    diagnostics = []
    paths = [
        source / name
        for name in (
            "source-inputs.json",
            "units.json",
            "evaluation.json",
            "astra-before-part1.json",
            "astra-before-part2.json",
            "extract-definition.json",
        )
    ] + [standards, Path(__file__)]
    for row in sorted(rows, key=lambda r: r["id"]):
        rid = row["id"]
        packet, group_units = packets[rid], units[rid]
        if group_units != build_units(packet) or row["pmid"] != packet["article_pmid"]:
            raise ValueError(f"Source identity mismatch: {rid}")
        result_path = source / "qwen35-9b-extract" / f"result-{rid}.json"
        paths.append(result_path)
        result = read(result_path)
        unit_by_id = {u["id"]: u for u in group_units}
        if set(row["axes"]) != set(AXES):
            raise ValueError(f"Missing audit axes: {rid}")
        for axis, state in row["axes"].items():
            if state not in STATES:
                raise ValueError(f"Unknown audit state: {rid}/{axis}")
            axis_counts[axis][state] += 1
        for issue in row["issues"]:
            if issue["category"] not in CATEGORIES:
                raise ValueError(f"Unknown error category: {rid}")
            refs = issue["source_unit_ids"]
            if any(ref not in unit_by_id for ref in refs):
                raise ValueError(f"Unknown source unit: {rid}")
            quote = issue["source_quote"]
            protocol_only = issue["category"] == "protocol" and not refs and not quote
            if not protocol_only and (
                not quote or not any(quote in unit_by_id[ref]["text"] for ref in refs)
            ):
                raise ValueError(f"Non-verbatim source quotation: {rid}/{quote}")
            candidate_quote = issue["candidate_quote"]
            if not candidate_quote or candidate_quote not in result["raw_output"]:
                raise ValueError(
                    f"Non-verbatim candidate quotation: {rid}/{candidate_quote}"
                )
        categories.update({issue["category"] for issue in row["issues"]})
        compact = result["payload"]
        raw_assembly = assemble(packet, group_units, compact)
        changed = False
        diagnostic_copy = copy.deepcopy(compact)
        if (
            isinstance(diagnostic_copy, dict)
            and set(diagnostic_copy) == {"fact"}
            and isinstance(diagnostic_copy["fact"], dict)
            and "reason" in diagnostic_copy["fact"]
        ):
            diagnostic_copy["reason"] = diagnostic_copy["fact"].pop("reason")
            changed = True
        moved_assembly = assemble(packet, group_units, diagnostic_copy)
        entity_rows = []
        fact = compact.get("fact") if isinstance(compact, dict) else None
        for entity in fact.get("entities", []) if isinstance(fact, dict) else []:
            entity_counts["items"] += 1
            if not isinstance(entity, list) or len(entity) != 4:
                entity_counts["invalid_tuple"] += 1
                continue
            kind, mention, ref, _roles = entity
            here = (
                isinstance(mention, str)
                and bool(mention)
                and mention in unit_by_id.get(ref, {}).get("text", "")
            )
            elsewhere = [
                u["id"]
                for u in group_units
                if isinstance(mention, str) and mention and mention in u["text"]
            ]
            state = (
                "exact_in_cited_unit"
                if here
                else "exact_elsewhere"
                if elsewhere
                else "not_exact_anywhere"
            )
            entity_counts[state] += 1
            if not here:
                link_state = "unanchored"
                hits = []
            elif kind not in {"gene", "drug", "disease"}:
                link_state = "outside_dictionary_types"
                hits = []
            else:
                hits = list(
                    {
                        json.dumps(h, sort_keys=True): h
                        for h in index.get((kind, key(mention)), [])
                    }.values()
                )
                link_state = (
                    "unique_candidate"
                    if len(hits) == 1
                    else "ambiguous"
                    if hits
                    else "unresolved"
                )
            link_counts[link_state] += 1
            entity_rows.append(
                {
                    "type": kind,
                    "mention": mention,
                    "cited_unit": ref,
                    "location_status": state,
                    "matching_units": elsewhere,
                    "dictionary_status": link_state,
                    "candidates": hits,
                }
            )
        diagnostics.append(
            {
                "id": rid,
                "parseable_json": isinstance(compact, dict),
                "original_structure_valid": raw_assembly["valid"],
                "reason_moved_in_diagnostic_copy": changed,
                "diagnostic_structure_valid": moved_assembly["valid"],
                "diagnostic_remaining_errors": moved_assembly["errors"],
                "entities": entity_rows,
            }
        )
    return {
        "version": "knowledge-error-audit-v1",
        "sample_count": 15,
        "review_type": "retrospective_assistant_audit_not_new_astra_gold",
        "scope": "007 compact extraction before verifier; historical outputs unchanged",
        "limits": [
            "Axis issue counts overlap; no observed issue is not proven accuracy.",
            "Unparseable outputs can support partial text review, not valid records.",
            "Protocol-only diagnostic does not assess semantic correctness.",
            "Dictionary links are candidates, not verified normalization or target UUIDs.",
            "The 15 development groups are not an independent test set.",
        ],
        "axis_counts": axis_counts,
        "category_group_counts": categories,
        "entity_location_counts": entity_counts,
        "dictionary_counts": link_counts,
        "protocol_diagnostic": {
            "operation": "Move fact.reason to root reason only in an in-memory copy",
            "changed_groups": [
                r["id"] for r in diagnostics if r["reason_moved_in_diagnostic_copy"]
            ],
            "before_valid": sum(r["original_structure_valid"] for r in diagnostics),
            "after_valid": sum(r["diagnostic_structure_valid"] for r in diagnostics),
            "newly_structure_valid": [
                r["id"]
                for r in diagnostics
                if r["diagnostic_structure_valid"] and not r["original_structure_valid"]
            ],
            "semantic_score": None,
        },
        "sources": [{"path": str(path), "sha256": sha(path)} for path in paths],
        "records": sorted(rows, key=lambda r: r["id"]),
        "mechanical_diagnostics": diagnostics,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--standards", type=Path, required=True)
    parser.add_argument("--audit", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.source, args.standards, args.audit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    freeze(args.output, result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "axis_counts",
                    "category_group_counts",
                    "entity_location_counts",
                    "dictionary_counts",
                    "protocol_diagnostic",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
