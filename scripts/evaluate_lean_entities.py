"""Evaluate frozen 010 entity reviews and script links without model calls."""

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_knowledge_candidates import freeze, read, sha
from scripts.evaluate_light_entities import core_score

EXPECTED_IDS = {f"K{i:02d}" for i in range(1, 16)}
VERDICTS = ("supported", "unsupported", "uncertain")


def indexed(rows, key, expected=None, label="records"):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows) or (expected is not None and set(result) != expected):
        raise ValueError(f"Duplicate or unexpected {label}")
    return result


def issues_indices(issue):
    return ([issue["entity_index"]] if "entity_index" in issue else []) + issue.get(
        "entity_indices", []
    )


def check_review(review, raw, assembled, reference, packet):
    rid = review["id"]
    payload = raw.get("payload")
    parsed = isinstance(payload, dict)
    if review["parse_failure"] == parsed or assembled["raw_json_valid"] != parsed:
        raise ValueError(f"Parse status mismatch: {rid}")
    rows = payload.get("entities", []) if parsed else []
    if not isinstance(rows, list) or len(rows) != assembled["raw_entity_count"]:
        raise ValueError(f"Raw entity count mismatch: {rid}")
    matched = review["matched_keys"]
    keys = {e["key"] for e in reference["entities"]}
    matches = review.get("matched_entity_indices", {})
    if len(set(matched)) != len(matched) or not set(matched) <= keys:
        raise ValueError(f"Unknown or repeated reference keys: {rid}")
    if set(matches) != set(matched) or (not parsed and matched):
        raise ValueError(f"Matched identities must name raw entity indices: {rid}")
    indices = [i for values in matches.values() for i in values]
    if any(not values for values in matches.values()):
        raise ValueError(f"Empty entity match: {rid}")
    for field in (
        "type_errors",
        "identity_errors",
        "granularity_issues",
        "extra_issues",
    ):
        for issue in review.get(field, []):
            indices.extend(issues_indices(issue))
    if any(type(i) is not int or not 0 <= i < len(rows) for i in indices):
        raise ValueError(f"Review entity index outside raw output: {rid}")
    for issue in review["type_errors"]:
        if rows[issue["entity_index"]].get("type") != issue["actual"]:
            raise ValueError(f"Type review differs from actual output: {rid}")
    passages = {p["id"]: p["text"] for p in packet["passages"]}
    covered = []
    for entity in assembled["entities"]:
        raw_indices = entity["raw_indices"]
        if not raw_indices or len(set(raw_indices)) != len(raw_indices):
            raise ValueError(f"Empty or duplicate raw indices: {entity['id']}")
        if entity["id"] != f"{rid}.e{raw_indices[0] + 1}":
            raise ValueError(
                f"Entity ID does not match first raw index: {entity['id']}"
            )
        for i in raw_indices:
            if type(i) is not int or not 0 <= i < len(rows):
                raise ValueError(f"Invalid assembled raw index: {rid}")
            if entity.get("status") != "invalid_entity" and any(
                entity.get(k) != rows[i].get(k) for k in ("type", "text")
            ):
                raise ValueError(
                    f"Assembly changed raw entity identity: {entity['id']}"
                )
        covered.extend(raw_indices)
        for location in entity["locations"]:
            text = passages.get(location["passage_id"])
            start, end = location["start"], location["end"]
            if (
                text is None
                or not 0 <= start < end <= len(text)
                or text[start:end] != entity["text"]
            ):
                raise ValueError(f"Invalid exact source location: {entity['id']}")
        found = entity.get("candidates", {}).get("candidates", [])
        candidates = indexed(found, "id", label=f"candidates/{entity['id']}")
        for candidate_id, guard in entity.get("candidate_guards", {}).items():
            if candidate_id not in candidates or guard["candidate_id"] != candidate_id:
                raise ValueError(f"Guard refers to unknown candidate: {entity['id']}")
        link = entity.get("link") or {}
        for field in ("candidate_id", "proposed_id"):
            if link.get(field) and link[field] not in candidates:
                raise ValueError(f"Link refers to unknown candidate: {entity['id']}")
        if link.get("candidate_id") and (
            not entity["locations"]
            or entity["candidate_guards"][link["candidate_id"]]["decision"] != "retain"
        ):
            raise ValueError(
                f"Selected link bypassed source/guard check: {entity['id']}"
            )
    if sorted(covered) != list(range(len(rows))):
        raise ValueError(f"Assembly must cover every raw index exactly once: {rid}")
    expected_schema = (
        parsed
        and set(payload) == {"entities"}
        and all(
            isinstance(e, dict)
            and set(e) == {"type", "text"}
            and e.get("type")
            in {
                "disease",
                "drug",
                "gene",
                "protein",
                "variant",
                "biomarker",
                "treatment",
            }
            and isinstance(e.get("text"), str)
            and bool(e["text"])
            for e in rows
        )
    )
    if assembled["schema_valid"] != expected_schema:
        raise ValueError(f"Schema status mismatch: {rid}")


def full_core_score(reference, review):
    matched = set(review["matched_keys"])
    chosen = min(
        reference["acceptable_required_sets"],
        key=lambda option: (len(set(option) - matched), len(option)),
    )
    return {"required": chosen, "missing": sorted(set(chosen) - matched)}


def summarize_scores(rows, reviews, records):
    type_issues = [e for r in reviews for e in r["type_errors"]]
    family = sum(
        e.get("certainty") == "family_granularity_boundary" for e in type_issues
    )
    entities = [e for r in records for e in r["entities"]]
    extras = [e for r in reviews for e in r.get("extra_issues", [])]
    return {
        "groups": len(rows),
        "complete_common_core_groups": sum(not r["missing"] for r in rows),
        "common_core_items": sum(len(r["required"]) for r in rows),
        "missing_common_core_items": sum(len(r["missing"]) for r in rows),
        "parse_failures": sum(r["parse_failure"] for r in reviews),
        "type_issues": len(type_issues),
        "clear_type_issues": len(type_issues) - family,
        "family_granularity_boundaries": family,
        "type_issue_groups": sum(bool(r["type_errors"]) for r in reviews),
        "identity_errors": sum(len(r["identity_errors"]) for r in reviews),
        "granularity_issues": sum(
            len(r.get("granularity_issues", [])) for r in reviews
        ),
        "delivered_entity_mentions": len(entities),
        "exact_source_mentions": sum(bool(e["locations"]) for e in entities),
        "nonexact_source_mentions": sum(not e["locations"] for e in entities),
        "extra_issue_groups": sum(bool(r.get("extra_issues")) for r in reviews),
        "extra_issues_by_kind": dict(
            Counter(e.get("kind", "unspecified") for e in extras)
        ),
    }


def review_links(records, reviews):
    entities = indexed(
        [e for r in records for e in r["entities"]], "id", label="entity IDs"
    )
    selected = {
        eid: e
        for eid, e in entities.items()
        if (e.get("link") or {}).get("candidate_id")
    }
    by_id = indexed(reviews, "entity_id", set(selected), "selected link reviews")
    for eid, review in by_id.items():
        if review["candidate_id"] != selected[eid]["link"]["candidate_id"]:
            raise ValueError(f"Reviewed link ID differs from actual selected ID: {eid}")
        if review["verdict"] not in VERDICTS:
            raise ValueError(f"Invalid link verdict: {eid}")
    result = {"selected": len(selected), **dict.fromkeys(VERDICTS, 0)}
    result.update(Counter(r["verdict"] for r in reviews))
    result["by_method"] = {}
    for method in sorted({e["link"]["method"] for e in selected.values()}):
        subset = [
            r for r in reviews if selected[r["entity_id"]]["link"]["method"] == method
        ]
        result["by_method"][method] = {
            "selected": len(subset),
            **{v: sum(r["verdict"] == v for r in subset) for v in VERDICTS},
        }
    unresolved = Counter()
    unresolved_statuses = Counter()
    candidate_counts = Counter()
    for eid, entity in entities.items():
        found = entity.get("candidates", {})
        candidates = found.get("candidates", [])
        retained = [
            c
            for c in candidates
            if entity.get("candidate_guards", {}).get(c["id"], {}).get("decision")
            == "retain"
        ]
        candidate_counts["mentions_with_retrieved_candidates"] += bool(candidates)
        candidate_counts["mentions_with_guard_retained_candidates"] += bool(retained)
        candidate_counts["retrieved_candidate_options"] += len(candidates)
        candidate_counts["guard_retained_candidate_options"] += len(retained)
        if eid in selected:
            continue
        unresolved_statuses[found.get("status", "unknown")] += 1
        if entity.get("status") == "invalid_entity":
            reason = "invalid_entity"
        elif found.get("status") == "unsupported_type":
            reason = "linker_does_not_support_entity_type"
        elif not entity["locations"]:
            reason = "not_exactly_located"
        elif found.get("truncated"):
            reason = "candidate_list_truncated"
        elif not candidates:
            reason = "no_dictionary_candidate"
        elif not retained:
            reason = "all_candidates_guarded"
        elif (entity.get("link") or {}).get("proposed_id"):
            reason = "preferred_candidate_guarded"
        elif len(retained) > 1:
            reason = "multiple_retained_candidates_no_automatic_selection"
        else:
            reason = "retained_candidate_without_direct_label_selection"
        unresolved[reason] += 1
    result.update(
        {
            "unresolved": len(entities) - len(selected),
            "unresolved_by_reason": dict(sorted(unresolved.items())),
            "unresolved_by_candidate_status": dict(sorted(unresolved_statuses.items())),
            "candidate_availability": dict(candidate_counts),
            "local_entities_retained": sum(
                bool(e.get("local_id")) for e in entities.values()
            ),
            "limits": "Candidate/guard retention is not identity verification. Unresolved does not mean wrong; zero errors with zero useful links is not success.",
        }
    )
    return result


def retained_links(old_records, old_links, new_records, new_links):
    old_verdicts = {r["entity_id"]: r["verdict"] for r in old_links}
    new_verdicts = {r["entity_id"]: r["verdict"] for r in new_links}
    current = {
        (r["id"], e.get("type"), e.get("text")): e
        for r in new_records
        for e in r["entities"]
    }
    retained, unresolved, absent_or_changed = [], [], []
    for record in old_records:
        for entity in record["entities"]:
            if old_verdicts.get(entity["id"]) != "supported":
                continue
            candidate_id = entity["link"]["candidate_id"]
            new = current.get((record["id"], entity["type"], entity["text"]))
            row = {
                "id": record["id"],
                "old_entity_id": entity["id"],
                "mention": entity["text"],
                "supported_candidate_id": candidate_id,
                "new_entity_id": new["id"] if new else None,
            }
            if (
                new
                and (new.get("link") or {}).get("candidate_id") == candidate_id
                and new_verdicts.get(new["id"]) == "supported"
            ):
                retained.append(row)
            elif new and not (new.get("link") or {}).get("candidate_id"):
                unresolved.append(row)
            else:
                absent_or_changed.append(row)
    return {
        "previously_supported": len(retained)
        + len(unresolved)
        + len(absent_or_changed),
        "retained_supported": len(retained),
        "now_unresolved": len(unresolved),
        "absent_or_changed": len(absent_or_changed),
        "unresolved_rows": unresolved,
        "absent_or_changed_rows": absent_or_changed,
        "matching_scope": "Same group, entity type, exact mention and selected standard ID; does not silently equate different aliases.",
    }


def guard_ablation(artifact, old_links):
    reviewed = indexed(old_links, "entity_id", label="009 link reviews")
    rows = indexed(
        artifact["links"], "entity_id", set(reviewed), "009 guard ablation links"
    )
    if len(rows) != 35:
        raise ValueError("Frozen 009 guard ablation must contain 35 selected links")
    counts = Counter()
    for eid, row in rows.items():
        old = reviewed[eid]
        if (
            row["review_verdict"] != old["verdict"]
            or row["guard"]["candidate_id"] != old["candidate_id"]
        ):
            raise ValueError(f"Guard ablation differs from frozen 009 link: {eid}")
        decision = row["guard"]["decision"]
        if decision not in {"retain", "unresolved"}:
            raise ValueError(f"Unexpected guard decision: {eid}")
        counts[f"{old['verdict']}_{decision}"] += 1
    return {
        "selected_009_links": len(rows),
        "counts": dict(sorted(counts.items())),
        "elapsed_seconds": artifact["elapsed_seconds"],
        "limitations": artifact["limitations"],
        "scope": "Frozen 009 links only; not the 010 extraction or a generalization test. Six wrong mentions represent two recurring identity errors.",
    }


def timing(root, base, assembled, old_evaluation, raw_results):
    new = read(root / "extract/summary.json")
    old_extract = read(base / "extract/summary.json")
    old_resolve = read(base / "disambiguate/summary.json")
    if new["processed"] != 15 or assembled["extra_model_calls"] != 0:
        raise ValueError(
            "Expected 15 extraction calls and no extra model calls for 010"
        )
    if not math.isclose(
        sum(r["seconds"] for r in raw_results),
        new["total_inference_seconds"],
        abs_tol=1e-4,
    ):
        raise ValueError("Inference timing differs from raw results")
    if sum(r["output_tokens"] for r in raw_results) != new["output_tokens"]:
        raise ValueError("Output token total differs from raw results")
    script = assembled["index_load_seconds"] + sum(
        r["script_seconds"] for r in assembled["records"]
    )
    values = {}
    for name, runs, script_seconds in (
        (
            "009",
            [old_extract, old_resolve],
            old_evaluation["timing"]["script_seconds_including_dictionary_setup"],
        ),
        ("010", [new], script),
    ):
        sessions = [s for run in runs for s in run["sessions"]]
        inference = sum(run["total_inference_seconds"] for run in runs)
        values[name] = {
            "task_model_calls": sum(run["processed"] for run in runs),
            "extraction_calls": runs[0]["processed"],
            "additional_resolution_calls": sum(run["processed"] for run in runs[1:]),
            "input_tokens": sum(run["input_tokens"] for run in runs),
            "output_tokens": sum(run["output_tokens"] for run in runs),
            "extraction_seconds": runs[0]["total_inference_seconds"],
            "additional_resolution_seconds": sum(
                run["total_inference_seconds"] for run in runs[1:]
            ),
            "script_seconds_including_dictionary_setup": script_seconds,
            "processing_seconds": inference + script_seconds,
            "model_load_seconds": sum(s.get("load_seconds", 0) for s in sessions),
            "warmup_seconds": sum(s.get("warmup_seconds", 0) for s in sessions),
            "warmup_calls_separate_from_task_calls": sum(
                s.get("warmup_status") == "classified" for s in sessions
            ),
            "runner_wall_seconds_including_startup": sum(
                s["wall_seconds"] for s in sessions
            ),
            "runner_plus_script_seconds": sum(s["wall_seconds"] for s in sessions)
            + script_seconds,
            "peak_process_mib": max(run["nvml_process_peak_mib"] for run in runs),
            "extraction_median_seconds": runs[0]["median_seconds"],
            "single_pass": all(run.get("measurement_repetitions") == 1 for run in runs),
        }
    values["010"]["dictionary_index_load_seconds"] = assembled["index_load_seconds"]
    values["010"]["per_record_script_seconds"] = sum(
        r["script_seconds"] for r in assembled["records"]
    )
    values["limits"] = (
        "Sequential batch=1 single development pass. Startup/cache differences are separate from processing. Excludes reference/review work; no measured human time savings or production-throughput claim."
    )
    return values


def build(root, base, new_paths, links_path):
    refs = indexed(
        read(root / "reference.json")["records"], "id", EXPECTED_IDS, "reference IDs"
    )
    if sha(root / "reference.json") != sha(base / "reference.json"):
        raise ValueError("010 reference must be the frozen 009 reference")
    packets = indexed(
        read(root / "source-inputs.json")["records"], "id", EXPECTED_IDS, "source IDs"
    )
    if sha(root / "source-inputs.json") != sha(base / "source-inputs.json"):
        raise ValueError("010 source paragraphs differ from frozen 009")
    assembled = read(root / "assembled.json")
    records = indexed(assembled["records"], "id", EXPECTED_IDS, "assembled IDs")
    reviews = indexed(
        [r for p in new_paths for r in read(p)["records"]],
        "id",
        EXPECTED_IDS,
        "review IDs",
    )
    old = read(base / "evaluation.json")
    old_rows = indexed(old["records"], "id", EXPECTED_IDS, "009 evaluation IDs")
    old_final = read(base / "final-entities.json")
    old_records = indexed(old_final["records"], "id", EXPECTED_IDS, "009 output IDs")
    raw = {
        rid: read(root / "extract" / f"result-{rid}.json")
        for rid in sorted(EXPECTED_IDS)
    }
    paired, common_new, common_old, full_new, full_old = [], [], [], [], []
    for rid in sorted(EXPECTED_IDS):
        if raw[rid]["id"] != rid:
            raise ValueError(f"Raw output ID mismatch: {rid}")
        check_review(reviews[rid], raw[rid], records[rid], refs[rid], packets[rid])
        previous = old_rows[rid]["light"]
        old_review = previous["review"]
        prior = core_score(refs[rid], old_review)
        if any(prior[k] != previous[k] for k in ("required", "missing")):
            raise ValueError(f"009 common-core scoring changed: {rid}")
        current = core_score(refs[rid], reviews[rid])
        common_new.append(current)
        common_old.append(prior)
        full_new.append(full_core_score(refs[rid], reviews[rid]))
        full_old.append(full_core_score(refs[rid], old_review))
        fixed_missing = sorted(
            set(previous["required"]) - set(reviews[rid]["matched_keys"])
        )
        paired.append(
            {
                "id": rid,
                "009": prior,
                "010": current,
                "fixed_009_required": previous["required"],
                "010_missing_fixed_009": fixed_missing,
                "different_legal_required_set": current["required"]
                != previous["required"],
                "full_reference_010": full_new[-1],
                "review": reviews[rid],
            }
        )
    fixed_denominator = sum(len(r["fixed_009_required"]) for r in paired)
    if fixed_denominator != 52:
        raise ValueError("Expected frozen 009 paired diagnostic denominator of 52")
    new_link_data = read(links_path)
    new_links = new_link_data.get("links", new_link_data.get("records"))
    old_links = old["link_reviews"]
    links = review_links(list(records.values()), new_links)
    retention = retained_links(
        list(old_records.values()), old_links, list(records.values()), new_links
    )
    review_summary = new_link_data.get("summary", {})
    if (
        "previously_supported_now_unresolved_count" in review_summary
        and review_summary["previously_supported_now_unresolved_count"]
        != retention["now_unresolved"]
    ):
        raise ValueError("Computed old-link retention differs from link review summary")
    old_link_counts = {
        "selected": len(old_links),
        **{v: sum(r["verdict"] == v for r in old_links) for v in VERDICTS},
    }
    all_entities = [e for r in records.values() for e in r["entities"]]
    duplicate_keys = []
    for rid, record in records.items():
        review = reviews[rid]
        for key, indices in review["matched_entity_indices"].items():
            ids = [
                e["id"]
                for e in record["entities"]
                if set(e["raw_indices"]) & set(indices)
            ]
            if len(ids) > 1:
                duplicate_keys.append({"id": rid, "key": key, "entity_ids": ids})
    paths = [
        root / name
        for name in (
            "reference.json",
            "source-inputs.json",
            "assembled.json",
            "plan.json",
            "guard-ablation.json",
            "extract/summary.json",
        )
    ] + [
        base / name
        for name in (
            "reference.json",
            "evaluation.json",
            "final-entities.json",
            "extract/summary.json",
            "disambiguate/summary.json",
        )
    ]
    paths += [
        *new_paths,
        links_path,
        Path(__file__),
        Path(__file__).with_name("evaluate_light_entities.py"),
    ]
    paths += [root / "extract" / f"result-{rid}.json" for rid in sorted(EXPECTED_IDS)]
    if (root / "executed-code.json").exists():
        paths.append(root / "executed-code.json")
    return {
        "version": "lean-entity-evaluation-v1",
        "review_kind": "assistant_source_review_not_new_astra_or_human_gold",
        "scope": "Entity identity and standard-link subtask only; preserved paragraphs are not extracted conditions or validated clinical facts.",
        "primary_definition": "Complete permitted common-core identity set per group /15, using frozen 009 core_score. Type errors remain separate; this is not a whole-knowledge pass rate. K01–K04 etoposide is excluded from this shared comparison only; reference is unchanged. Alternative set chosen by fewest missing then shortest set, retaining reference order on ties.",
        "scores": {
            "009": summarize_scores(
                common_old,
                [old_rows[r]["light"]["review"] for r in sorted(EXPECTED_IDS)],
                list(old_records.values()),
            ),
            "010": summarize_scores(
                common_new, list(reviews.values()), list(records.values())
            ),
        },
        "fixed_009_paired_diagnostic": {
            "items": fixed_denominator,
            "009_missing": sum(len(r["009"]["missing"]) for r in paired),
            "010_missing": sum(len(r["010_missing_fixed_009"]) for r in paired),
            "010_complete_groups": sum(not r["010_missing_fixed_009"] for r in paired),
            "different_legal_required_sets": [
                r["id"] for r in paired if r["different_legal_required_set"]
            ],
            "limits": "A different complete permitted finding is not a clinical error. This diagnostic does not replace the primary chosen-set score.",
        },
        "secondary_full_reference": {
            name: {
                "items": sum(len(r["required"]) for r in rows),
                "missing": sum(len(r["missing"]) for r in rows),
                "complete_groups": sum(not r["missing"] for r in rows),
            }
            for name, rows in (("009", full_old), ("010", full_new))
        },
        "complexity": {
            "raw_entity_mentions": sum(r["raw_entity_count"] for r in records.values()),
            "assembled_entity_mentions": len(all_entities),
            "exact_type_text_duplicates_removed": sum(
                len(e["raw_indices"]) - 1 for e in all_entities
            ),
            "repeated_reference_identity_mentions": duplicate_keys,
            "repeated_identity_excess_mentions": sum(
                len(r["entity_ids"]) - 1 for r in duplicate_keys
            ),
            "review_issue_operations_proxy": {
                field: sum(len(r.get(field, [])) for r in reviews.values())
                for field in (
                    "type_errors",
                    "identity_errors",
                    "granularity_issues",
                    "extra_issues",
                )
            },
            "limits": "Repeated identity includes aliases and overlapping combination mentions; it is an inspection proxy, not a count of distinct mistakes. Review issue counts are not measured user operations or time saved.",
        },
        "links": {"009": old_link_counts, "010": links},
        "link_value_comparison": {
            "supported_selected_change": links["supported"]
            - old_link_counts["supported"],
            "unsupported_selected_change": links["unsupported"]
            - old_link_counts["unsupported"],
            "paired_previous_supported": retention,
            "note": "Aggregate useful-link retention must accompany wrong-ID and core-omission counts. New mentions differ, so this is not a paired retention rate; the separate guard ablation is paired.",
        },
        "link_reviews": new_links,
        "link_review_summary": review_summary,
        "guard_009_ablation": guard_ablation(
            read(root / "guard-ablation.json"), old_links
        ),
        "timing": timing(root, base, assembled, old, list(raw.values())),
        "condition_accuracy_measured": False,
        "records": paired,
        "sources": [{"path": str(p), "sha256": sha(p)} for p in paths],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--baseline-root",
        type=Path,
        default=Path("var/experiments/light-entities-20261029"),
    )
    parser.add_argument("--new-review", type=Path, nargs="+", required=True)
    parser.add_argument("--link-review", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, args.baseline_root, args.new_review, args.link_review)
    freeze(args.root / "evaluation.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("scores", "links", "fixed_009_paired_diagnostic", "timing")
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
