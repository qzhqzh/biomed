"""Aggregate frozen source-based reviews; no model calls or production writes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_knowledge_candidates import freeze, read, sha


def core_score(reference, review):
    keys = {e["key"] for e in reference["entities"]}
    matched = set(review["matched_keys"])
    if not matched <= keys:
        raise ValueError("Review uses unknown reference keys")
    alternatives = reference["acceptable_required_sets"]
    # Shared comparison excludes the newly requested separate etoposide detail.
    common = [
        [
            k
            for k in option
            if not (
                reference["id"] in {"K01", "K02", "K03", "K04"} and k == "etoposide"
            )
        ]
        for option in alternatives
    ]
    chosen = min(common, key=lambda option: (len(set(option) - matched), len(option)))
    return {"required": chosen, "missing": sorted(set(chosen) - matched)}


def build(root, baseline_path, new_paths, links_path):
    reference = read(root / "reference.json")
    refs = {r["id"]: r for r in reference["records"]}
    old_reviews = read(baseline_path)["records"]
    new_reviews = [r for path in new_paths for r in read(path)["records"]]
    final = read(root / "final-entities.json")
    aggregate = {}
    paired = {}
    for name, reviews, records in (
        ("baseline", old_reviews, final["baseline_records"]),
        ("light", new_reviews, final["records"]),
    ):
        if len(reviews) != 15 or {r["id"] for r in reviews} != set(refs):
            raise ValueError("Each method requires exactly the same 15 reviews")
        by_id = {r["id"]: r for r in records}
        rows = []
        for review in reviews:
            rid = review["id"]
            source = by_id[rid]
            if bool(review["parse_failure"]) == bool(source["raw_json_valid"]):
                raise ValueError(f"Parse status mismatch: {name}/{rid}")
            for issue in review["type_errors"]:
                entity = source["entities"][issue["entity_index"]]
                if issue["actual"] != entity["type"]:
                    raise ValueError(f"Type review mismatch: {name}/{rid}")
            score = core_score(refs[rid], review)
            row = {"id": rid, **score, "review": review}
            rows.append(row)
            paired.setdefault(rid, {})[name] = row
        type_errors = [e for r in reviews for e in r["type_errors"]]
        aggregate[name] = {
            "groups": 15,
            "parse_failures": sum(r["parse_failure"] for r in reviews),
            "common_core_items": sum(len(r["required"]) for r in rows),
            "missing_common_core_items": sum(len(r["missing"]) for r in rows),
            "complete_common_core_groups": sum(not r["missing"] for r in rows),
            "type_issues": len(type_errors),
            "family_granularity_boundaries": sum(
                e.get("certainty") == "family_granularity_boundary" for e in type_errors
            ),
            "type_issue_groups": sum(bool(r["type_errors"]) for r in reviews),
            "identity_errors": sum(len(r["identity_errors"]) for r in reviews),
            "delivered_entity_mentions": sum(len(r["entities"]) for r in records),
            "exact_source_mentions": sum(
                bool(e["locations"]) for r in records for e in r["entities"]
            ),
            "background_extra_groups": sum(
                bool(r.get("extra_issues")) for r in reviews
            ),
        }
    link_review = read(links_path)
    link_rows = link_review.get("links", link_review.get("records"))
    selected = {
        e["id"]: e
        for r in final["records"]
        for e in r["entities"]
        if e.get("link", {}).get("candidate_id")
    }
    if len(link_rows) != len(selected) or {r["entity_id"] for r in link_rows} != set(
        selected
    ):
        raise ValueError("Every selected link must be reviewed exactly once")
    for r in link_rows:
        if r["candidate_id"] != selected[r["entity_id"]]["link"]["candidate_id"]:
            raise ValueError("Reviewed standard ID differs from model/script output")
        if r["verdict"] not in {"supported", "unsupported", "uncertain"}:
            raise ValueError("Invalid link review verdict")
    link_scores = {}
    for method in ("unique_label", "9b_disambiguation"):
        rows = [
            r for r in link_rows if selected[r["entity_id"]]["link"]["method"] == method
        ]
        link_scores[method] = {
            "selected": len(rows),
            **{
                k: sum(r["verdict"] == k for r in rows)
                for k in ("supported", "unsupported", "uncertain")
            },
        }
    extraction = read(root / "extract/summary.json")
    resolution = read(root / "disambiguate/summary.json")
    script_seconds = (
        final["index_load_seconds"]
        + sum(r["script_seconds"] for r in final["records"])
        + final["finalize_seconds"]
    )
    timing = {
        "extraction_seconds": extraction["total_inference_seconds"],
        "disambiguation_seconds": resolution["total_inference_seconds"],
        "disambiguation_groups": resolution["processed"],
        "script_seconds_including_dictionary_setup": script_seconds,
        "processing_seconds": extraction["total_inference_seconds"]
        + resolution["total_inference_seconds"]
        + script_seconds,
        "baseline_extraction_and_assembly_seconds": 593.47,
        "load_and_warmup_seconds": sum(
            s.get("load_seconds", 0) + s.get("warmup_seconds", 0)
            for run in (extraction, resolution)
            for s in run["sessions"]
        ),
        "runner_wall_seconds_including_startup": sum(
            s["wall_seconds"]
            for run in (extraction, resolution)
            for s in run["sessions"]
        ),
        "peak_process_mib": max(
            extraction["nvml_process_peak_mib"], resolution["nvml_process_peak_mib"]
        ),
        "extraction_median_seconds": extraction["median_seconds"],
        "limits": "Research runner, sequential batch=1. No reference-authoring/review time included. Startup is reported separately; full-fact and entity-only tasks are not equal workloads.",
    }
    modifiers = [m for r in final["records"] for m in r["modifiers"]]
    paths = [
        root / "reference.json",
        root / "plan.json",
        root / "assembled.json",
        root / "final-entities.json",
        baseline_path,
        *new_paths,
        links_path,
        Path(__file__),
        Path("scripts/light_entity_pilot.py"),
        Path("apps/curation/light_entity_linking.py"),
    ]
    return {
        "version": "light-entity-evaluation-v1",
        "review_kind": "assistant_reference_not_human_gold",
        "scope": "Entity subtask only; original 006/007 scores unchanged",
        "scores": aggregate,
        "links": link_scores,
        "link_reviews": link_rows,
        "timing": timing,
        "modifiers": {
            "count": len(modifiers),
            "exact_source": sum(bool(m["locations"]) for m in modifiers),
            "lossless_shape_adapters": sum(
                bool(m.get("lossless_adapter")) for m in modifiers
            ),
            "condition_completeness_measured": False,
        },
        "records": [{"id": rid, **paired[rid]} for rid in sorted(paired)],
        "sources": [{"path": str(p), "sha256": sha(p)} for p in paths],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline-review", type=Path, required=True)
    parser.add_argument("--new-review", type=Path, nargs="+", required=True)
    parser.add_argument("--link-review", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, args.baseline_review, args.new_review, args.link_review)
    freeze(args.root / "evaluation.json", result)
    print(
        json.dumps(
            {k: result[k] for k in ("scores", "links", "timing", "modifiers")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
