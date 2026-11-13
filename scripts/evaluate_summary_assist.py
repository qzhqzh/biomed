"""Evaluate a separately generated summary as an aid, never as source evidence."""

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.core_knowledge_projection import project
from apps.curation.decision import digest
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.evaluate_lean_entities import check_review as check_legacy_entities
from scripts.evaluate_summary_ablation import (
    arm_timing,
    common_digest,
    verify_quotes,
)
from scripts.evaluate_summary_ablation import (
    check_review as check_relation_review,
)
from scripts.light_entity_pilot import KINDS, locate
from scripts.summary_ablation_pilot import compare_common_schema
from scripts.summary_assist_pilot import (
    PREFIX,
    SUMMARY_PROMPT,
    string_sha,
    summary_prompt,
)

VERSION = "summary-assist-evaluation-v1"
ENTITY_BASE = Path("var/experiments/lean-entities-20261029")
RELATION_BASE = Path("var/experiments/summary-ablation-20261029")
IDS = {f"K{i:02}" for i in range(1, 16)}
ARMS = ("source_only", "summary_assisted")


def pair_scores(rows, stage):
    pairs = {
        name: []
        for name in ("both_pass", "source_only", "summary_assisted_only", "both_fail")
    }
    names = {
        (True, True): "both_pass",
        (True, False): "source_only",
        (False, True): "summary_assisted_only",
        (False, False): "both_fail",
    }
    paper_delta = Counter()
    for row in rows:
        before, after = (row[stage][arm]["core_usable"] for arm in ARMS)
        pairs[names[(before, after)]].append(row["id"])
        paper_delta[row["article"]["pmid"]] += int(after) - int(before)
    wins, losses = pairs["summary_assisted_only"], pairs["source_only"]
    return {
        "pairs": {key: {"count": len(ids), "ids": ids} for key, ids in pairs.items()},
        "net_group_gain": len(wins) - len(losses),
        "winning_article_ids": sorted(
            {r["article"]["pmid"] for r in rows if r["id"] in wins}
        ),
        "net_positive_article_ids": sorted(
            pmid for pmid, change in paper_delta.items() if change > 0
        ),
        "per_article_net_change": dict(sorted(paper_delta.items())),
    }


def decide_default(paired, misbindings, timing):
    no_new_misbinding = not any(misbindings.values())
    gains = {
        stage: paired[stage]["net_group_gain"] for stage in ("entities", "relations")
    }
    quality_stages = [
        stage
        for stage in gains
        if gains[stage] >= 2
        and len(paired[stage]["winning_article_ids"]) >= 2
        and all(change >= 0 for change in gains.values())
    ]
    baseline, assisted = (
        timing["source_only_total_seconds"],
        timing["summary_assisted_total_seconds"],
    )
    speed_fraction = (baseline - assisted) / baseline
    quality_gate = bool(quality_stages) and no_new_misbinding
    speed_gate = (
        all(change >= 0 for change in gains.values())
        and no_new_misbinding
        and speed_fraction >= 0.10
    )
    return {
        "default_summary_enabled": quality_gate or speed_gate,
        "quality_gate_passed": quality_gate,
        "quality_gate_eligible_stages": quality_stages,
        "speed_gate_passed": speed_gate,
        "no_new_core_misbinding": no_new_misbinding,
        "measured_speedup_fraction": speed_fraction,
        "rule": "A stage gains at least two net core-usable groups, winning groups come from at least two articles, the other stage does not decline, and no new core misbinding; alternatively both stages do not decline, no new core misbinding, and shared summary+entity+relation task time improves by at least 10%.",
        "interpretation": "Measured evidence supports the preregistered default-use threshold on this development sample."
        if quality_gate or speed_gate
        else "This experiment has not demonstrated that a summary is worth enabling by default; it does not establish that summaries are universally ineffective.",
    }


def source_entity_view(packet, raw):
    payload = raw.get("payload")
    entities = payload.get("entities") if isinstance(payload, dict) else None
    schema_valid = (
        raw.get("status") == "classified"
        and isinstance(payload, dict)
        and set(payload) == {"entities"}
        and isinstance(entities, list)
    )
    if not isinstance(entities, list):
        return {"schema_valid": False, "entities": []}
    output = []
    for index, entity in enumerate(entities):
        valid = (
            isinstance(entity, dict)
            and set(entity) == {"type", "text"}
            and isinstance(entity.get("type"), str)
            and entity.get("type") in KINDS
            and isinstance(entity.get("text"), str)
            and bool(entity["text"])
        )
        schema_valid = schema_valid and valid
        output.append(
            {
                "raw_index": index,
                "raw": entity,
                "locations": locate(packet, entity.get("text")) if valid else [],
                "location_scope": "original_passages_only",
                "standard_link_status": "not_measured",
            }
        )
    return {"schema_valid": schema_valid, "entities": output}


def validate_raw(path, input_row, manifest, reviewed_sha):
    raw = read(path)
    require(
        raw["id"] == input_row["id"]
        and raw["input_sha256"] == input_row["input_sha256"]
        and raw["config_sha256"] == manifest["config_sha256"]
        and sha(path) == reviewed_sha,
        "Reviewed result identity changed",
    )
    if raw["status"] == "classified":
        require(
            parse(raw["raw_output"]) == raw["payload"],
            "Raw output and parsed result differ",
        )
    else:
        require(raw["payload"] is None, "Do not rescue partial model output")
    return raw


def validate_manifest(folder, inputs, definition):
    manifest = read(folder / "manifest.json")
    require(
        manifest["records"] == 15
        and manifest["config_sha256"] == digest(manifest["config"])
        and manifest["input_sha256"] == digest(inputs)
        and manifest["definition_sha256"] == digest(definition)
        and manifest["config"]["definition"] == definition,
        "Run manifest differs from frozen inputs/config",
    )
    require(
        {r["id"] for r in inputs["records"]} == IDS and len(inputs["records"]) == 15,
        "Run denominator changed",
    )
    for row in inputs["records"]:
        require(
            row["input_sha256"]
            == digest({k: v for k, v in row.items() if k != "input_sha256"}),
            "Input hash differs",
        )
    require(read(folder / "inputs.json") == inputs, "Runner input copy differs")
    for name, expected in manifest["config"]["code_sha256"].items():
        require(sha(Path(name)) == expected, "Inference code changed")
    return manifest


def load_runs(root):
    specs = {
        "A_entities": (
            root / "baseline/entity/extract",
            root / "baseline/entity/inputs.json",
            root / "baseline/entity/definition.json",
        ),
        "A_relations": (
            root / "baseline/relation/extract",
            root / "baseline/relation/inputs.json",
            root / "baseline/relation/definition.json",
        ),
        "summary": (
            root / "summary",
            root / "summary-inputs.json",
            root / "summary-definition.json",
        ),
        "B_entities": (
            root / "entities/extract",
            root / "entity-inputs.json",
            root / "entity-definition.json",
        ),
        "B_relations": (
            root / "relations",
            root / "relation-inputs.json",
            root / "relation-definition.json",
        ),
    }
    runs = {}
    for key, (folder, input_path, definition_path) in specs.items():
        inputs, definition = read(input_path), read(definition_path)
        # Baseline preparation freezes the manifest/results; its runner input copy stays at the historical source.
        validation_folder = (
            ENTITY_BASE / "extract"
            if key == "A_entities"
            else RELATION_BASE / "extract"
            if key == "A_relations"
            else folder
        )
        manifest = validate_manifest(validation_folder, inputs, definition)
        require(
            manifest == read(folder / "manifest.json"),
            "Copied baseline manifest differs",
        )
        summary_path = (
            ENTITY_BASE / "extract/summary.json"
            if key == "A_entities"
            else RELATION_BASE / "extract/summary.json"
            if key == "A_relations"
            else folder / "summary.json"
        )
        runs[key] = {
            "folder": folder,
            "inputs": indexed(inputs["records"], key + " inputs"),
            "definition": definition,
            "manifest": manifest,
            "summary": read(summary_path),
            "summary_path": summary_path,
        }
    for stage in ("entities", "relations"):
        old, new = runs["A_" + stage], runs["B_" + stage]
        require(
            old["definition"] == new["definition"], "Downstream system prompt changed"
        )
        require(
            old["manifest"]["config"] == new["manifest"]["config"],
            "Downstream model or generation configuration changed",
        )
    summary_config = runs["summary"]["manifest"]["config"]
    require(
        {
            k: v
            for k, v in summary_config.items()
            if k not in {"definition", "max_new_tokens"}
        }
        == {
            k: v
            for k, v in runs["A_entities"]["manifest"]["config"].items()
            if k not in {"definition", "max_new_tokens"}
        },
        "Summary model/runtime changed",
    )
    require(
        summary_config["max_new_tokens"] == 512
        and runs["summary"]["definition"]["system_prompt"] == SUMMARY_PROMPT,
        "Summary generation differs from plan",
    )
    return runs, [path for spec in specs.values() for path in spec[1:]] + [
        r["folder"] / "manifest.json" for r in runs.values()
    ] + [r["summary_path"] for r in runs.values()]


def validate_plan(root, reference):
    plan = read(root / "plan.json")
    require(
        plan["sample_count"] == 15
        and set(plan["group_ids"]) == IDS
        and plan["scored_new_calls"]
        == {"summary": 15, "entities": 15, "relations": 15, "total": 45},
        "Experiment design changed",
    )
    require(
        plan["prefix_template"] == PREFIX
        and plan["reference_sha256"] == sha(root / "reference.json"),
        "Frozen plan or reference changed",
    )
    for source in plan["sources"]:
        require(
            sha(Path(source["path"]))
            == sha(root / source["frozen_path"])
            == source["sha256"],
            "Baseline source/copy changed",
        )
    for path, expected in reference["source_hashes"].items():
        require(sha(Path(path)) == expected, "Reviewed historical source changed")
    for source in reference["entity_scoring"]["existing_script_equivalence"]["sources"]:
        require(
            sha(Path(source["path"])) == source["sha256"],
            "Common projection source changed",
        )
    for name in ("inputs", "definition"):
        require(
            sha(root / f"summary-{name}.json") == plan[f"summary_{name}_sha256"],
            "Summary preparation changed",
        )
    require(
        (root / "packets.json").read_bytes()
        == (RELATION_BASE / "packets.json").read_bytes(),
        "Original relation packet bytes changed",
    )
    for name in ("source-inputs.json", "source-context.json", "guard-metadata.json"):
        require(
            (root / "entities" / name).read_bytes()
            == (ENTITY_BASE / name).read_bytes(),
            "Entity source/guard bytes changed",
        )
    return plan


def check_entity_review(review, raw, assembled, legacy_ref, entity_ref, packet):
    check_legacy_entities(
        {**review, "id": packet["id"]}, raw, assembled, legacy_ref, packet
    )
    verify_quotes(review, packet)
    for key in ("core_usable", "raw_core_usable"):
        require(
            type(review.get(key)) is bool, "Missing effective/raw entity core score"
        )
    for key in (
        "raw_core_errors",
        "core_errors",
        "existing_script_projection_check",
        "new_core_misbindings",
    ):
        require(isinstance(review.get(key), list), f"Missing entity review {key}")
    require(
        review.get("wrong_links", {}).get("status") == "not_measured",
        "Standard link accuracy was not measured",
    )
    matched = set(review["matched_keys"])
    required = min(
        entity_ref["current_core_required_sets"],
        key=lambda keys: (len(set(keys) - matched), len(keys)),
    )
    missing = sorted(set(required) - matched)
    for field in ("core_errors", "raw_core_errors", "new_core_misbindings"):
        for error in review[field]:
            require(
                error.get("kind") and error.get("problem"),
                "Unexplained entity core error",
            )
            if "entity_index" in error:
                require(
                    type(error["entity_index"]) is int
                    and 0 <= error["entity_index"] < assembled["raw_entity_count"],
                    "Core error entity index invalid",
                )
    require(
        review["core_usable"]
        == (not review["parse_failure"] and not missing and not review["core_errors"]),
        "Effective entity score differs from reviewed errors/identity",
    )
    require(
        review["raw_core_usable"]
        == (
            not review["parse_failure"]
            and not missing
            and not review["raw_core_errors"]
        ),
        "Raw entity score differs from reviewed errors/identity",
    )
    probe_packet = {**packet, "entities": assembled["entities"]}
    projections = []
    for check in review["existing_script_projection_check"]:
        name = check["source_name"]
        require(
            any(e.get("text") == name for e in assembled["entities"]),
            "Projection cannot borrow an entity absent from this arm",
        )
        projection = project({"fact": {"drug_names": [name]}}, probe_packet)
        change = next(c for c in projection["changes"] if c["name"] == name)
        target = change["target"]
        term = next(t for t in projection[target] if t["name"] == name)
        require(target == check["target"], "Reviewed projection target differs")
        if "unresolved_kind" in check:
            require(
                term.get("kind") == check["unresolved_kind"],
                "Reviewed class diagnostic differs",
            )
        if "remaining_types" in check:
            require(
                term.get("types", []) == check["remaining_types"],
                "Reviewed remaining type differs",
            )
        projections.append(
            {
                "source_name": name,
                "reviewed_check": check,
                "actual_projection": projection,
            }
        )
    fixed = entity_ref["fixed_52_required_keys"]
    return {
        "core_usable": review["core_usable"],
        "raw_core_usable": review["raw_core_usable"],
        "current_identity_complete": not missing and not review["parse_failure"],
        "current_required_keys": required,
        "current_missing_keys": missing,
        "historical_52_required_keys": fixed,
        "historical_52_missing_keys": sorted(set(fixed) - matched),
        "review": review,
        "raw_result": raw,
        "source_entity_locations": source_entity_view(packet, raw),
        "common_projection_checks": projections,
        "assembled": assembled,
        "wrong_links": {"status": "not_measured"},
    }


def validate_assembled(path):
    artifact = read(path)
    historical_equivalence = []
    for name, expected in artifact["source_hashes"].items():
        current = sha(Path(name))
        if current == expected:
            continue
        require(
            path.resolve() == (ENTITY_BASE / "assembled.json").resolve()
            and Path(name).suffix == ".py",
            "Entity assembly data or new source changed",
        )
        snapshot_path = ENTITY_BASE / "executed-code.json"
        snapshots = [
            entry for entry in read(snapshot_path)["files"] if entry["path"] == name
        ]
        require(len(snapshots) == 1, "No unique frozen historical Python source")
        historical = snapshots[0]
        require(
            historical["sha256"] == expected
            and hashlib.sha256(historical["text"].encode()).hexdigest() == expected,
            "Historical source snapshot does not match original assembly hash",
        )
        require(
            ast.dump(ast.parse(historical["text"], type_comments=True))
            == ast.dump(ast.parse(Path(name).read_text(), type_comments=True)),
            "Historical Python behavior changed",
        )
        historical_equivalence.append(
            {
                "path": name,
                "original_sha256": expected,
                "current_sha256": current,
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": sha(snapshot_path),
                "verification": "Frozen historical source SHA verified; Python AST including type comments unchanged. Source formatting differs, not the guard logic.",
            }
        )
    artifact["historical_code_equivalence"] = historical_equivalence
    require(
        artifact["extra_model_calls"] == 0, "Unexpected entity resolution model calls"
    )
    return artifact, indexed(artifact["records"], "assembled entities")


def stage_totals(rows, stage, arm):
    values = [r[stage][arm] for r in rows]
    result = {
        "groups": len(rows),
        "effective_core_usable": sum(r["core_usable"] for r in values),
    }
    if stage == "entities":
        result.update(
            raw_core_usable=sum(r["raw_core_usable"] for r in values),
            current_identity_complete=sum(
                r["current_identity_complete"] for r in values
            ),
            historical_52_items=sum(
                len(r["historical_52_required_keys"]) for r in values
            ),
            historical_52_missing_items=sum(
                len(r["historical_52_missing_keys"]) for r in values
            ),
            historical_52_complete_groups=sum(
                not r["historical_52_missing_keys"] for r in values
            ),
            raw_core_errors=sum(len(r["review"]["raw_core_errors"]) for r in values),
            effective_core_errors=sum(len(r["review"]["core_errors"]) for r in values),
            type_errors=sum(len(r["review"]["type_errors"]) for r in values),
            identity_errors=sum(len(r["review"]["identity_errors"]) for r in values),
            unlocated_mentions=sum(
                not e["locations"]
                for r in values
                for e in r["source_entity_locations"]["entities"]
            ),
            wrong_links={"status": "not_measured"},
        )
    else:
        result.update(
            core_and_anchor=sum(
                r["core_usable"] and r["review"]["axes"]["anchor_focus"] is True
                for r in values
            ),
            diagnostic_core_ready=sum(r["diagnostic_core_ready"] for r in values),
            strict_core_ready=sum(r["strict_core_ready"] for r in values),
            axes={
                key: {
                    "pass": sum(r["review"]["axes"][key] is True for r in values),
                    "fail": sum(r["review"]["axes"][key] is False for r in values),
                    "not_applicable": sum(
                        r["review"]["axes"][key] is None for r in values
                    ),
                }
                for key in values[0]["review"]["axes"]
            },
        )
    return result


def build(root):
    reference, review = read(root / "reference.json"), read(root / "review.json")
    require(
        reference["model"] == review["model"] == "gpt-6-astra"
        and review["status"] == "complete",
        "Completed actual Astra review required",
    )
    require(
        review["reference_sha256"] == sha(root / "reference.json"),
        "Reference changed after review",
    )
    plan = validate_plan(root, reference)
    runs, paths = load_runs(root)
    packets = indexed(read(root / "packets.json")["records"], "packets")
    refs, reviews = (
        indexed(reference["records"], "reference"),
        indexed(review["records"], "review"),
    )
    require(set(refs) == set(reviews) == set(packets) == IDS, "Need the same 15 groups")
    require(list(packets) == plan["group_ids"], "Source order changed")
    for run in runs.values():
        require(list(run["inputs"]) == plan["group_ids"], "Run order changed")
    legacy_refs = indexed(
        read(ENTITY_BASE / "reference.json")["records"], "legacy entity reference"
    )
    old_entity_eval = indexed(
        read(ENTITY_BASE / "evaluation.json")["records"], "010 evaluation"
    )
    old_relation_reviews = indexed(
        read(RELATION_BASE / "review.json")["records"], "014 review"
    )
    assembly_docs, assemblies = {}, {}
    for arm, path in (
        ("source_only", ENTITY_BASE / "assembled.json"),
        ("summary_assisted", root / "entities/assembled.json"),
    ):
        assembly_docs[arm], assemblies[arm] = validate_assembled(path)
        require(set(assemblies[arm]) == IDS, "Assembly denominator changed")
        paths.append(path)
    assist = read(root / "assisted-inputs-manifest.json")
    require(
        assist["status"] == "prepared"
        and assist["denominator"] == 15
        and assist["prefix_template"] == PREFIX
        and assist["summary_manifest_sha256"] == sha(root / "summary/manifest.json")
        and assist["summary_inputs_sha256"] == sha(root / "summary-inputs.json"),
        "Assistance manifest changed",
    )
    metadata = {(r["stage"], r["id"]): r for r in assist["records"]}
    require(
        len(metadata) == len(assist["records"]) == 30, "Assisted manifest pairs differ"
    )
    rows, raw_by_run = [], {key: [] for key in runs}
    started = perf_counter()
    for rid in plan["group_ids"]:
        packet, ref, judged = packets[rid], refs[rid], reviews[rid]
        baseline_relation = ref["relations"]["baseline"]
        require(
            baseline_relation.get("new_core_misbindings") == []
            and {
                key: value
                for key, value in baseline_relation.items()
                if key != "new_core_misbindings"
            }
            == old_relation_reviews[rid],
            "Relation A must be the actual 014 without-summary review",
        )
        require(
            ref["entities"]["fixed_52_required_keys"]
            == old_entity_eval[rid]["fixed_009_required"],
            "Historical 52 denominator changed",
        )
        verify_quotes(judged, packet)
        summary_run = runs["summary"]
        summary_path = summary_run["folder"] / f"result-{rid}.json"
        summary_raw = validate_raw(
            summary_path,
            summary_run["inputs"][rid],
            summary_run["manifest"],
            judged["summary"]["result_sha256"],
        )
        require(
            summary_run["inputs"][rid]["prompt"] == summary_prompt(packet),
            "Summary input included something beyond original source/title",
        )
        require(
            summary_raw["status"] == "classified"
            and isinstance(summary_raw["payload"], dict)
            and set(summary_raw["payload"]) == {"summary"}
            and isinstance(summary_raw["payload"]["summary"], str)
            and bool(summary_raw["payload"]["summary"].strip()),
            "No invalid summary rescue",
        )
        require(
            type(judged["summary"].get("supported")) is bool
            and isinstance(judged["summary"].get("errors"), list)
            and bool(judged["summary"].get("brief")),
            "Incomplete summary review",
        )
        summary_text = summary_raw["payload"]["summary"]
        row = {
            "id": rid,
            "article": packet["article"],
            "original_passages": packet["passages"],
            "summary": {
                "raw_result": summary_raw,
                "review": judged["summary"],
                "counts_as_source": False,
            },
            "entities": {},
            "relations": {},
        }
        raw_by_run["summary"].append(summary_raw)
        paths.append(summary_path)
        for stage, singular in (("entities", "entity"), ("relations", "relation")):
            baseline_review, new_review = ref[stage]["baseline"], judged[stage]
            require(
                isinstance(new_review.get("new_core_misbindings"), list),
                "Missing new core-misbinding audit",
            )
            a_input, b_input = (
                runs["A_" + stage]["inputs"][rid],
                runs["B_" + stage]["inputs"][rid],
            )
            prefix = PREFIX.format(summary=summary_text)
            require(
                b_input["prompt"] == prefix + a_input["prompt"],
                "Assistance must prepend only the unedited summary; original input must stay byte-for-byte",
            )
            expected_metadata = {
                "id": rid,
                "stage": singular,
                "summary_result_sha256": sha(summary_path),
                "summary_text_sha256": string_sha(summary_text),
                "summary_words": len(summary_text.split()),
                "baseline_input_sha256": a_input["input_sha256"],
                "baseline_prompt_sha256": string_sha(a_input["prompt"]),
                "prefix_sha256": string_sha(prefix),
                "assisted_input_sha256": b_input["input_sha256"],
                "original_prompt_unchanged": True,
            }
            require(
                metadata.get((singular, rid)) == expected_metadata,
                "Assisted input provenance changed",
            )
            pair_raw = []
            for arm, key, judgment in (
                ("source_only", "A_" + stage, baseline_review),
                ("summary_assisted", "B_" + stage, new_review),
            ):
                run = runs[key]
                path = run["folder"] / f"result-{rid}.json"
                raw = validate_raw(
                    path, run["inputs"][rid], run["manifest"], judgment["result_sha256"]
                )
                pair_raw.append(raw)
                raw_by_run[key].append(raw)
                paths.append(path)
                if stage == "entities":
                    row[stage][arm] = check_entity_review(
                        judgment,
                        raw,
                        assemblies[arm][rid],
                        legacy_refs[rid],
                        ref["entities"],
                        packet,
                    )
                else:
                    check_relation_review(judgment, packet)
                    fact = (raw.get("payload") or {}).get("fact")
                    if "common_fields_sha256" in judgment:
                        require(
                            common_digest(fact) == judgment["common_fields_sha256"],
                            "Reviewed relation common fields changed",
                        )
                    mechanical = compare_common_schema(packet, raw, raw)["arms"][
                        "without_summary"
                    ]
                    strict = (
                        mechanical["raw"]["status"] == "common_structurally_valid"
                        and not mechanical["raw"]["format_changes"]
                    )
                    diagnostic = (
                        mechanical["diagnostic"]["status"]
                        == "common_structurally_valid"
                    )
                    row[stage][arm] = {
                        "core_usable": judgment["structured_common_core_usable"],
                        "review": judgment,
                        "raw_result": raw,
                        "mechanical": mechanical,
                        "strict_core_ready": strict
                        and judgment["structured_common_core_usable"],
                        "diagnostic_core_ready": diagnostic
                        and judgment["structured_common_core_usable"],
                    }
            row[stage]["inference_seconds"] = {
                arm: raw["seconds"] for arm, raw in zip(ARMS, pair_raw, strict=True)
            }
        rows.append(row)
    script_seconds = perf_counter() - started
    timings = {
        key: arm_timing(run["summary"], raw_by_run[key], run["manifest"])
        for key, run in runs.items()
    }
    costs = {
        "source_only_total_seconds": timings["A_entities"]["inference_seconds"]
        + timings["A_relations"]["inference_seconds"],
        "summary_assisted_total_seconds": sum(
            timings[k]["inference_seconds"]
            for k in ("summary", "B_entities", "B_relations")
        ),
        "summary_plus_entities_seconds": timings["summary"]["inference_seconds"]
        + timings["B_entities"]["inference_seconds"],
        "summary_plus_relations_seconds": timings["summary"]["inference_seconds"]
        + timings["B_relations"]["inference_seconds"],
        "summary_shared_count_in_total": 1,
    }
    require(
        abs(costs["source_only_total_seconds"] - 910.370371) < 0.00001
        and abs(
            costs["source_only_total_seconds"]
            - reference["decision_gate"]["speed_route_B"][
                "baseline_task_inference_seconds"
            ]
        )
        < 0.00001,
        "Locked task-time baseline differs",
    )
    paired = {stage: pair_scores(rows, stage) for stage in ("entities", "relations")}
    scores = {
        stage: {arm: stage_totals(rows, stage, arm) for arm in ARMS} for stage in paired
    }
    require(
        all(s["historical_52_items"] == 52 for s in scores["entities"].values()),
        "Historical entity denominator is not 52",
    )
    frozen = reference["baseline_summary"]
    require(
        scores["entities"]["source_only"]["effective_core_usable"]
        == frozen["entity_gate_identity_and_type_core_usable_groups"]
        and scores["entities"]["source_only"]["raw_core_usable"]
        == frozen["entity_raw_identity_and_type_core_usable_groups"]
        and scores["relations"]["source_only"]["effective_core_usable"]
        == frozen["relation_common_core_usable_groups"],
        "Frozen baseline score differs",
    )
    misbindings = {
        stage: [
            {"id": row["id"], "article_pmid": row["article"]["pmid"], **issue}
            for row in rows
            for issue in row[stage]["summary_assisted"]["review"][
                "new_core_misbindings"
            ]
        ]
        for stage in paired
    }
    decision = decide_default(paired, misbindings, costs)
    decision["preregistered_gate"] = reference["decision_gate"]
    paths += [
        root / name
        for name in (
            "reference.json",
            "review.json",
            "plan.json",
            "packets.json",
            "assisted-inputs-manifest.json",
            "entities/source-inputs.json",
            "entities/source-context.json",
            "entities/guard-metadata.json",
        )
    ]
    paths += [
        ENTITY_BASE / "evaluation.json",
        ENTITY_BASE / "reference.json",
        ENTITY_BASE / "executed-code.json",
        RELATION_BASE / "evaluation.json",
        RELATION_BASE / "review.json",
        Path(__file__),
        Path("scripts/summary_assist_pilot.py"),
        Path("scripts/evaluate_summary_ablation.py"),
        Path("scripts/lean_entity_pilot.py"),
        Path("apps/curation/core_knowledge_projection.py"),
        Path("apps/curation/entity_link_guard.py"),
    ]
    for optional in (
        "executed-code.json",
        "summary-preflight.json",
        "assisted-preflight.json",
    ):
        if (root / optional).exists():
            paths.append(root / optional)
    return {
        "version": VERSION,
        "reference_version": reference["version"],
        "state": "research_comparison_not_production",
        "denominator": 15,
        "scores": scores,
        "paired": paired,
        "new_core_misbindings": misbindings,
        "decision": decision,
        "summary_quality": {
            "supported_groups": sum(r["summary"]["review"]["supported"] for r in rows),
            "unsupported_ids": [
                r["id"] for r in rows if not r["summary"]["review"]["supported"]
            ],
            "note": "Summary fidelity is a diagnostic, not downstream extraction recall or independent source evidence.",
        },
        "timing": {
            **costs,
            "runs": timings,
            "new_scored_attempts": sum(
                len(raw_by_run[k]) for k in ("summary", "B_entities", "B_relations")
            ),
            "new_inference_calls": sum(
                r["status"] != "input_too_long"
                for k in ("summary", "B_entities", "B_relations")
                for r in raw_by_run[k]
            ),
            "new_repair_calls": 0,
            "comparison_script_seconds": script_seconds,
            "entity_assembly_script_seconds": {
                arm: doc["index_load_seconds"]
                + sum(r["script_seconds"] for r in doc["records"])
                for arm, doc in assembly_docs.items()
            },
            "scope": "Warm task inference only for the decision. Summary is counted once in the combined two-stage total; separate stage costs each include it. Startup, warmup, script processing and research review are separate.",
        },
        "records": rows,
        "validation": {
            "original_passages_only_for_locations_and_quotes": True,
            "summary_prefix_only": True,
            "original_relation_entity_inventory_fixed": True,
            "new_entities_not_used_by_relations": True,
            "downstream_system_prompts_and_model_configs_unchanged": True,
            "raw_outputs_and_old_artifacts_unmodified": True,
            "historical_code_equivalence": assembly_docs["source_only"][
                "historical_code_equivalence"
            ],
        },
        "limits": [
            "Fifteen reused groups from five articles, historical single-run controls; no general effectiveness or causal claim.",
            "Entity gate uses the same source guards/type projection on each arm's own output. Raw type repairs already handled by that script remain diagnostics, not added business benefit.",
            "Historical 52-item identity coverage is separate from current specific core identity/type quality; generic chemotherapy recall alone does not qualify.",
            "Standard-ID correctness was not re-audited; candidate source/guard integrity is checked, wrong links remain not_measured.",
            "Independent paired entity/relation tasks share summary costs; this is not a new entity-to-relation cascaded workflow and does not replace the 013 knowledge export.",
        ],
        "sources": [
            {"path": str(path), "sha256": sha(path)} for path in dict.fromkeys(paths)
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    target = args.root / "evaluation.json"
    require(not target.exists(), "Evaluation already frozen; refusing to overwrite")
    result = build(args.root)
    freeze(target, result)
    print(
        json.dumps(
            {"scores": result["scores"], "decision": result["decision"]},
            ensure_ascii=False,
        )
    )
