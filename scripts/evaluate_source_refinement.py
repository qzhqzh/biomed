"""Compare source-only type rules and one relation-prompt refinement independently."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.decision import digest
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.evaluate_summary_ablation import (
    AXES,
    arm_timing,
    check_review,
    common_digest,
    verify_quotes,
)
from scripts.source_refinement_pilot import (
    BASE,
    CONSTRAINTS,
    DEFAULT_ROOT,
    ENTITY_BASE,
    refined_prompt,
)
from scripts.summary_ablation_pilot import compare_common_schema

VERSION = "source-refinement-evaluation-v1"
ARMS = ("baseline", "refined")
IDS = {f"K{i:02}" for i in range(1, 16)}


def check_sources(sources):
    for path, expected in sources.items():
        require(sha(Path(path)) == expected, f"Source changed: {path}")


def validate_plan(root):
    plan, reference = read(root / "plan.json"), read(root / "reference.json")
    check_sources(reference["source_hashes"])
    check_sources({s["path"]: s["sha256"] for s in plan["sources"]})
    require(
        plan["reference_sha256"] == sha(root / "reference.json"), "Reference changed"
    )
    require(plan["audit_sha256"] == sha(root / "failure-audit.json"), "Audit changed")
    require(plan["prompt_change"] == CONSTRAINTS, "Unplanned prompt constraints")
    for name in ("inputs.json", "packets.json"):
        require(
            (root / name).read_bytes() == (BASE / name).read_bytes(),
            f"Input changed: {name}",
        )
    definitions = [read(p / "definition.json") for p in (BASE, root)]
    require(
        definitions[1]["system_prompt"]
        == refined_prompt(definitions[0]["system_prompt"]),
        "Prompt changed beyond appended constraints",
    )
    dataset = read(root / "inputs.json")
    inputs = indexed(dataset["records"], "inputs")
    manifests = [read(p / "extract/manifest.json") for p in (BASE, root)]
    require(
        {k: v for k, v in manifests[0]["config"].items() if k != "definition"}
        == {k: v for k, v in manifests[1]["config"].items() if k != "definition"},
        "Unplanned generation change",
    )
    for manifest, definition in zip(manifests, definitions, strict=True):
        require(
            manifest["records"] == 15
            and manifest["input_sha256"] == digest(dataset)
            and manifest["definition_sha256"] == digest(definition)
            and manifest["config"]["definition"] == definition
            and manifest["config_sha256"] == digest(manifest["config"]),
            "Inference manifest differs",
        )
        check_sources(manifest["config"]["code_sha256"])
        require(
            all(
                manifest["config"].get(k) == v
                for k, v in plan["generation"].items()
                if k != "gpu_index"
            ),
            "Generation differs from plan",
        )
    for item in inputs.values():
        require(
            item["input_sha256"]
            == digest({k: v for k, v in item.items() if k != "input_sha256"}),
            "Input digest differs",
        )
    for item in read(root / "executed-code.json")["files"]:
        require(sha(Path(item["path"])) == item["sha256"], "Executed source changed")
    for auxiliary in reference["auxiliary_definition_sources"]:
        path = Path(auxiliary["path"])
        require(sha(path) == auxiliary["file_sha256"], "Auxiliary source changed")
        source = indexed(read(path)["records"], "auxiliary passages")[
            auxiliary["passage_id"]
        ]
        require(
            all(
                source[key] == auxiliary[key]
                for key in ("pmid", "version_id", "source_sha256", "locator")
            ),
            "Auxiliary article/version changed",
        )
        verify_quotes(auxiliary, {"passages": [source]})
    return plan, reference, inputs, manifests


def relation_arm(packet, path, judgment, input_row, manifest):
    raw = read(path)
    require(
        raw["id"] == packet["id"]
        and raw["input_sha256"] == input_row["input_sha256"]
        and raw["config_sha256"] == manifest["config_sha256"]
        and sha(path) == judgment["result_sha256"],
        "Reviewed relation output changed",
    )
    if raw["status"] == "classified":
        require(parse(raw["raw_output"]) == raw["payload"], "Raw JSON was modified")
    else:
        require(raw["payload"] is None, "Partial result was rescued")
    check_review(judgment, packet)
    require(
        isinstance(judgment["new_core_misbindings"], list), "Missing misbinding review"
    )
    fact = (raw.get("payload") or {}).get("fact")
    if "common_fields_sha256" in judgment:
        require(
            common_digest(fact) == judgment["common_fields_sha256"],
            "Reviewed fact digest differs",
        )
    # Both arms have no statement. Use the existing without-summary checker twice.
    mechanical = compare_common_schema(packet, raw, raw)["arms"]["without_summary"]
    strict = (
        mechanical["raw"]["status"] == "common_structurally_valid"
        and not mechanical["raw"]["format_changes"]
    )
    diagnostic = mechanical["diagnostic"]["status"] == "common_structurally_valid"
    core = judgment["structured_common_core_usable"]
    return {
        "core_usable": core,
        "raw_result": raw,
        "result_sha256": sha(path),
        "review": judgment,
        "mechanical": mechanical,
        "strict_schema_valid": strict,
        "diagnostic_schema_valid": diagnostic,
        "strict_core_ready": strict and core,
        "diagnostic_core_ready": diagnostic and core,
    }


def paired(rows, stage, field="core_usable"):
    buckets = {
        name: [] for name in ("both_pass", "baseline_only", "refined_only", "both_fail")
    }
    names = {
        (True, True): "both_pass",
        (True, False): "baseline_only",
        (False, True): "refined_only",
        (False, False): "both_fail",
    }
    for row in rows:
        values = tuple(row[stage][arm][field] for arm in ARMS)
        buckets[names[values]].append(row["id"])
    return {key: {"count": len(ids), "ids": ids} for key, ids in buckets.items()}


def relation_scores(rows, arm):
    values = [r["relations"][arm] for r in rows]
    scores = {
        key: sum(r[key] for r in values)
        for key in (
            "core_usable",
            "strict_schema_valid",
            "diagnostic_schema_valid",
            "strict_core_ready",
            "diagnostic_core_ready",
        )
    }
    scores["core_and_anchor"] = sum(
        r["core_usable"] and r["review"]["axes"]["anchor_focus"] is True for r in values
    )
    scores["raw_with_format_adaptation_schema_valid"] = sum(
        r["mechanical"]["raw"]["status"] == "common_structurally_valid" for r in values
    )
    scores["axes"] = {
        axis: {
            "pass": sum(r["review"]["axes"][axis] is True for r in values),
            "fail": sum(r["review"]["axes"][axis] is False for r in values),
            "not_applicable": sum(r["review"]["axes"][axis] is None for r in values),
        }
        for axis in AXES
    }
    scores["error_occurrences_by_kind"] = dict(
        Counter(e["kind"] for r in values for e in r["review"]["errors"])
    )
    return {"groups": len(values), **scores}


def entity_record(
    root, packet, original, typed, reference, review, source_packet, context
):
    from apps.curation.source_entity_types import identity, refine

    require(
        typed["original_entities"] == original["entities"]
        and typed["article"] == original["article"],
        "Original entity inventory changed",
    )
    same_article = [
        p for p in context if identity(p) == identity(source_packet["article"])
    ]
    replay = refine(source_packet, original["entities"], same_article)
    require(
        {k: v for k, v in typed.items() if k != "script_seconds"} == replay,
        "Type projection does not reproduce",
    )
    before, after = [
        indexed(values, "entity inventory")
        for values in (original["entities"], typed["entities"])
    ]
    require(
        set(before) == set(after)
        and all(
            before[eid]["text"] == after[eid]["text"]
            and before[eid]["locations"] == after[eid]["locations"]
            for eid in before
        ),
        "Entity names or locations changed",
    )
    baseline = reference["baseline"]
    raw_path = ENTITY_BASE / "extract" / f"result-{packet['id']}.json"
    require(
        sha(raw_path) == review["result_sha256"] == baseline["result_sha256"],
        "Unchanged entity model result differs",
    )
    for key in (
        "matched_keys",
        "matched_entity_indices",
        "parse_failure",
        "raw_core_usable",
        "raw_core_errors",
    ):
        if key in review:
            require(
                review[key] == baseline[key],
                f"Unchanged raw entity assessment differs: {key}",
            )
    post = review["postprocess"]
    require(
        post["artifact_sha256"] == sha(root / "entity-types.json")
        and post["raw_preserved"] is True
        and post["source_names_unchanged"] is True,
        "Type review provenance differs",
    )
    require(
        type(post["supported"]) is bool
        and isinstance(post["side_effects"], list)
        and bool(post["brief"]),
        "Incomplete postprocessing review",
    )
    fields = ("entity_id", "from_type", "to_type", "rule_id")
    changes = sorted(tuple(c[k] for k in fields) for c in typed["changes"])
    require(
        changes == sorted(tuple(c[k] for k in fields) for c in post["type_changes"]),
        "Reviewed changes differ from actual changes",
    )
    for change in post["type_changes"] + post["link_changes"]:
        require(
            type(change["supported"]) is bool
            and bool(change["English_source_evidence"]),
            "Changed type/link requires source review",
        )
    changed_links = {
        eid for eid in before if before[eid].get("link") != after[eid].get("link")
    }
    require(
        changed_links == {c["entity_id"] for c in post["link_changes"]},
        "Link changes not completely reviewed",
    )
    for change in post["link_changes"]:
        eid = change["entity_id"]
        require(
            change["before"] == before[eid].get("link")
            and change["after"] == after[eid].get("link"),
            "Reviewed link transition differs",
        )
    evidence_packet = {"passages": same_article}
    verify_quotes(typed["changes"], evidence_packet)
    verify_quotes(review, evidence_packet)
    verify_quotes(baseline, evidence_packet)
    require(
        type(review["core_usable"]) is bool
        and isinstance(review["core_errors"], list)
        and isinstance(review["new_core_misbindings"], list),
        "Missing entity core review",
    )
    require(
        not review["core_usable"] or not review["core_errors"],
        "Entity core pass has errors",
    )
    require(
        not post["supported"]
        or (
            not post["side_effects"]
            and all(c["supported"] for c in post["type_changes"] + post["link_changes"])
        ),
        "Supported projection has unsupported changes",
    )
    current_complete = not baseline["current_core_missing_keys"]
    return {
        "baseline": {
            "core_usable": baseline["core_usable"],
            "raw_core_usable": baseline["raw_core_usable"],
            "current_identity_complete": current_complete,
        },
        "refined": {
            "core_usable": review["core_usable"],
            "raw_core_usable": baseline["raw_core_usable"],
            "current_identity_complete": current_complete,
        },
        "baseline_review": baseline,
        "review": review,
        "raw_result": read(raw_path),
        "original_entities": typed["original_entities"],
        "derived_entities": typed["entities"],
        "changes": typed["changes"],
        "checked": typed["checked"],
        "review_scope": "Script-derived type view; model extraction and source names unchanged.",
        "standard_link_accuracy": "not_measured",
    }


def rule_decisions(rows, types):
    decisions = []
    for rule_id in types["rules"]:
        selected = [
            (r, c)
            for r in rows
            for c in r["entities"]["changes"]
            if c["rule_id"] == rule_id
        ]
        groups = sorted({r["id"] for r, _ in selected})
        supported, side_effects = [], []
        for row, change in selected:
            review = row["entities"]["review"]
            item = next(
                c
                for c in review["postprocess"]["type_changes"]
                if c["entity_id"] == change["entity_id"] and c["rule_id"] == rule_id
            )
            supported.extend((item["supported"], review["postprocess"]["supported"]))
            supported.extend(
                c["supported"]
                for c in review["postprocess"]["link_changes"]
                if c["entity_id"] == change["entity_id"]
            )
            # Unattributed side effects conservatively count against an affected rule.
            side_effects.extend(
                {"id": row["id"], "error": e}
                for e in review["postprocess"]["side_effects"]
                + review["new_core_misbindings"]
                if e.get("rule_id", rule_id) == rule_id
            )
            if (
                row["entities"]["baseline"]["core_usable"]
                and not row["entities"]["refined"]["core_usable"]
            ):
                side_effects.append(
                    {
                        "id": row["id"],
                        "error": {"kind": "previously_usable_group_regressed"},
                    }
                )
        no_new_errors = not side_effects
        decisions.append(
            {
                "rule_id": rule_id,
                "changes": len(selected),
                "affected_groups": groups,
                "source_supported": bool(selected) and all(supported),
                "no_new_errors": no_new_errors,
                "side_effects": side_effects,
                "adoptable": bool(selected) and all(supported) and no_new_errors,
                "scope": "Supported on these frozen source examples; no unseen-document generalization established.",
            }
        )
    return decisions


def build(root):
    root, started = Path(root), perf_counter()
    plan, reference, inputs, manifests = validate_plan(root)
    reviewed, types = read(root / "review.json"), read(root / "entity-types.json")
    require(
        reference["model"] == reviewed["model"] == "gpt-6-astra"
        and reviewed["status"] == "complete"
        and reviewed["reference_sha256"] == sha(root / "reference.json"),
        "Completed matching Astra review required",
    )
    packets, refs, reviews, typed, originals = [
        indexed(data["records"], label)
        for data, label in (
            (read(root / "packets.json"), "packets"),
            (reference, "reference"),
            (reviewed, "review"),
            (types, "typed entities"),
            (read(ENTITY_BASE / "assembled.json"), "original entities"),
        )
    ]
    require(
        set(inputs)
        == set(packets)
        == set(refs)
        == set(reviews)
        == set(typed)
        == set(originals)
        == IDS,
        "Same 15 groups required",
    )
    require(list(inputs) == list(packets) == plan["group_ids"], "Group order changed")
    old_entity_reviews = indexed(
        read(Path("var/experiments/summary-assist-20261029/reference.json"))["records"],
        "015 reference",
    )
    old_relation_reviews = indexed(read(BASE / "review.json")["records"], "014 review")
    check_sources(types["source_hashes"])
    require(types["model_calls"] == 0, "Type rules must not call a model")
    source_paths = types["input_paths"]
    require(
        Path(source_paths["source_inputs"]).resolve()
        == (ENTITY_BASE / "source-inputs.json").resolve()
        and Path(source_paths["assembled"]).resolve()
        == (ENTITY_BASE / "assembled.json").resolve(),
        "Type rules use different entity inputs",
    )
    require(
        all(path in types["source_hashes"] for path in source_paths.values()),
        "Missing type input hashes",
    )
    source_packets = indexed(
        read(ENTITY_BASE / "source-inputs.json")["records"], "entity source packets"
    )
    context = read(Path(source_paths["context"]))["records"]
    rows, paths = [], []
    for rid, packet in packets.items():
        ref, review = refs[rid], reviews[rid]
        require(
            ref["entities"]["baseline"]
            == old_entity_reviews[rid]["entities"]["baseline"],
            "Frozen entity baseline changed",
        )
        old_relation = dict(ref["relations"]["baseline"])
        require(
            old_relation.pop("new_core_misbindings") == [],
            "Baseline misbinding annotation changed",
        )
        require(
            old_relation == old_relation_reviews[rid],
            "Frozen relation baseline changed",
        )
        verify_quotes(ref["relations"], packet)
        entities = entity_record(
            root,
            packet,
            originals[rid],
            typed[rid],
            ref["entities"],
            review["entities"],
            source_packets[rid],
            context,
        )
        relations = {
            arm: relation_arm(
                packet,
                folder / "extract" / f"result-{rid}.json",
                judgment,
                inputs[rid],
                manifest,
            )
            for arm, folder, judgment, manifest in zip(
                ARMS,
                (BASE, root),
                (ref["relations"]["baseline"], review["relations"]),
                manifests,
                strict=True,
            )
        }
        errors = [
            {e["kind"] for e in relations[arm]["review"]["errors"] if e["blocking"]}
            for arm in ARMS
        ]
        rows.append(
            {
                "id": rid,
                "article": packet["article"],
                "original_passages": packet["passages"],
                "entities": entities,
                "relations": relations,
                "error_changes": {
                    "resolved_blocking_kinds": sorted(errors[0] - errors[1]),
                    "introduced_blocking_kinds": sorted(errors[1] - errors[0]),
                    "persistent_blocking_kinds": sorted(errors[0] & errors[1]),
                },
            }
        )
        paths.extend(
            folder / "extract" / f"result-{rid}.json"
            for folder in (BASE, root, ENTITY_BASE)
        )
    scores = {
        "entities": {
            arm: {
                "groups": 15,
                **{
                    key: sum(r["entities"][arm][key] for r in rows)
                    for key in (
                        "core_usable",
                        "raw_core_usable",
                        "current_identity_complete",
                    )
                },
                "changed_entities": sum(len(r["entities"]["changes"]) for r in rows)
                if arm == "refined"
                else 0,
                "standard_link_accuracy": "not_measured",
            }
            for arm in ARMS
        },
        "relations": {arm: relation_scores(rows, arm) for arm in ARMS},
    }
    require(
        scores["entities"]["baseline"]["core_usable"]
        == reference["baseline_summary"][
            "entity_gate_identity_and_type_core_usable_groups"
        ]
        and scores["relations"]["baseline"]["core_usable"]
        == reference["baseline_summary"]["relation_common_core_usable_groups"],
        "Baseline totals differ",
    )
    pairs = {stage: paired(rows, stage) for stage in ("entities", "relations")}
    pairs.update(
        {
            field: paired(rows, "relations", field)
            for field in ("strict_core_ready", "diagnostic_core_ready")
        }
    )
    wins, losses = (
        pairs["relations"]["refined_only"]["ids"],
        pairs["relations"]["baseline_only"]["ids"],
    )
    new_errors = [
        {"id": r["id"], "error": error}
        for r in rows
        for error in r["relations"]["refined"]["review"]["new_core_misbindings"]
    ]
    decision = {
        "entity_rules": rule_decisions(rows, types),
        "relation_prompt": {
            "adoptable": bool(wins) and not losses and not new_errors,
            "repaired_ids": wins,
            "regressed_ids": losses,
            "new_core_misbindings": new_errors,
            "rule": "Repair at least one old failure, retain every previously passing group, and introduce no new core misbinding; measured cost is reported separately.",
        },
    }
    timings = {
        arm: arm_timing(
            read(folder / "extract/summary.json"),
            [r["relations"][arm]["raw_result"] for r in rows],
            manifest,
        )
        for arm, folder, manifest in zip(ARMS, (BASE, root), manifests, strict=True)
    }
    paths += [
        root / name
        for name in (
            "plan.json",
            "reference.json",
            "review.json",
            "entity-types.json",
            "failure-audit.json",
            "executed-code.json",
            "inputs.json",
            "packets.json",
            "definition.json",
            "extract/manifest.json",
            "extract/summary.json",
        )
    ]
    paths += [
        Path(__file__),
        Path("scripts/source_refinement_pilot.py"),
        Path("scripts/refine_source_entity_types.py"),
        Path("scripts/summary_ablation_pilot.py"),
        Path("scripts/evaluate_summary_ablation.py"),
        BASE / "extract/summary.json",
        ENTITY_BASE / "assembled.json",
    ]
    return {
        "version": VERSION,
        "state": "research_comparison_not_production",
        "denominator": 15,
        "scores": scores,
        "paired": pairs,
        "decision": decision,
        "timing": {
            "relations": timings,
            "entity_script_seconds": types["script_seconds"],
            "entity_projection_seconds": sum(
                r["script_seconds"] for r in typed.values()
            ),
            "evaluation_script_seconds": perf_counter() - started,
            "new_model_calls": 15,
            "entity_model_calls": 0,
            "summary_calls": 0,
            "relation_delta_seconds": timings["refined"]["inference_seconds"]
            - timings["baseline"]["inference_seconds"],
            "review_time_included": False,
        },
        "records": rows,
        "validation": {
            "inputs_byte_identical": True,
            "prompt_only_appended_constraints": True,
            "fixed_original_entity_inventory_in_relation_inputs": True,
            "baseline_reviews_unchanged": True,
            "auxiliary_sources_used_only_for_entity_rules": reference[
                "auxiliary_definition_sources"
            ],
        },
        "limits": plan["limits"]
        + [
            "Astra model reference is not human gold. Rules and relation prompt are independent; this is not a new end-to-end run.",
            "Only changed link handling is reviewed; general standard-ID accuracy was not measured.",
        ],
        "sources": {str(path): sha(path) for path in paths},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    require(
        not (args.root / "evaluation.json").exists(), "Frozen evaluation already exists"
    )
    result = build(args.root)
    freeze(args.root / "evaluation.json", result)
    print(
        json.dumps(
            {"scores": result["scores"], "decision": result["decision"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
