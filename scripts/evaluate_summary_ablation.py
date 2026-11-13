"""Paired statement ablation: audit frozen inputs and aggregate existing reviews."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.decision import digest
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.summary_ablation_pilot import (
    BASE,
    COMMON_KEYS,
    compare_common_schema,
    remove_statement_prompt,
)

VERSION = "summary-ablation-evaluation-v1"
ARMS = ("with_summary", "without_summary")
AXES = (
    "drug_identity_roles",
    "cancer_link",
    "biomarker_relation",
    "type_purity",
    "numeric_condition_scope_binding",
    "evidence_support",
    "anchor_focus",
)


def common_digest(fact):
    return hashlib.sha256(
        json.dumps(
            fact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def verify_quotes(value, packet):
    passages = {p["id"]: p for p in packet["passages"]}

    def visit(node):
        if isinstance(node, dict):
            if "quote" in node:
                require(node.get("passage_id") in passages, "Quote has unknown passage")
                passage, quote = passages[node["passage_id"]], node["quote"]
                require(
                    isinstance(quote, str) and bool(quote) and quote in passage["text"],
                    "Quote is not exact source English",
                )
                if "start" in node or "end" in node:
                    start, end = node.get("start"), node.get("end")
                    require(
                        type(start) is int
                        and type(end) is int
                        and 0 <= start < end <= len(passage["text"])
                        and passage["text"][start:end] == quote,
                        "Quote offsets differ",
                    )
                if "passage_text_sha256" in node:
                    require(
                        hashlib.sha256(passage["text"].encode()).hexdigest()
                        == node["passage_text_sha256"],
                        "Quoted paragraph hash differs",
                    )
                if "source_sha256" in node:
                    require(
                        passage["source_sha256"] == node["source_sha256"],
                        "Quoted article hash differs",
                    )
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)


def check_review(review, packet):
    require(
        type(review.get("structured_common_core_usable")) is bool,
        "Missing common-core decision",
    )
    require(set(review.get("axes", {})) == set(AXES), "Review axes differ")
    require(
        all(value is None or type(value) is bool for value in review["axes"].values()),
        "Axes must be bool or null",
    )
    for field in ("errors", "diagnostics", "deferred"):
        require(isinstance(review.get(field), list), f"Missing review {field}")
    require(bool(review.get("brief")), "Missing review explanation")
    for error in review["errors"]:
        require(
            all(error.get(k) for k in ("kind", "field", "problem"))
            and type(error.get("blocking")) is bool,
            "Incomplete reviewed error",
        )
    if review["structured_common_core_usable"]:
        require(
            not any(error["blocking"] for error in review["errors"]),
            "Blocking error with core pass",
        )
    verify_quotes(review, packet)


def paired(records, selector):
    result = {
        name: []
        for name in (
            "both_pass",
            "with_summary_only",
            "without_summary_only",
            "both_fail",
            "not_applicable",
        )
    }
    labels = {
        (True, True): "both_pass",
        (True, False): "with_summary_only",
        (False, True): "without_summary_only",
        (False, False): "both_fail",
    }
    for row in records:
        values = tuple(selector(row["arms"][arm]) for arm in ARMS)
        result["not_applicable" if None in values else labels[values]].append(row["id"])
    return {name: {"count": len(ids), "ids": ids} for name, ids in result.items()}


def summarize(records, arm):
    rows = [r["arms"][arm] for r in records]
    reviews = [r["review"] for r in rows]
    axes = {}
    for axis in AXES:
        values = [r["axes"][axis] for r in reviews]
        axes[axis] = {
            "pass": sum(v is True for v in values),
            "fail": sum(v is False for v in values),
            "not_applicable": sum(v is None for v in values),
            "applicable_denominator": sum(v is not None for v in values),
        }
    return {
        "groups": len(rows),
        "structured_common_core_usable": sum(
            r["structured_common_core_usable"] for r in reviews
        ),
        "core_and_anchor": sum(
            r["structured_common_core_usable"] and r["axes"]["anchor_focus"] is True
            for r in reviews
        ),
        "strict_schema_valid": sum(r["strict_schema_valid"] for r in rows),
        "raw_with_format_adaptation_schema_valid": sum(
            r["mechanical"]["raw"]["status"] == "common_structurally_valid"
            for r in rows
        ),
        "diagnostic_schema_valid": sum(r["diagnostic_schema_valid"] for r in rows),
        "strict_core_ready": sum(r["strict_core_ready"] for r in rows),
        "diagnostic_core_ready": sum(r["diagnostic_core_ready"] for r in rows),
        "axes": axes,
        "error_occurrences_by_kind": dict(
            Counter(e["kind"] for r in reviews for e in r["errors"])
        ),
        "error_groups_by_kind": {
            kind: [
                r["id"]
                for r in records
                if any(e["kind"] == kind for e in r["arms"][arm]["review"]["errors"])
            ]
            for kind in sorted({e["kind"] for r in reviews for e in r["errors"]})
        },
        "deferred_groups": sum(bool(r["deferred"]) for r in reviews),
    }


def validate_experiment(root):
    plan, reference = read(root / "plan.json"), read(root / "reference.json")
    for source in plan["sources"]:
        source_path = (
            root / "reference.json"
            if source["path"].endswith("biomed-summary-ablation-reference.json")
            else Path(source["path"])
        )
        require(sha(source_path) == source["sha256"], "Planned source changed")
    for name, expected in reference["source_hashes"].items():
        require(sha(BASE / name) == expected, "Reviewed baseline source changed")
    for name in ("inputs.json", "packets.json"):
        require(
            (root / name).read_bytes() == (BASE / name).read_bytes(),
            f"Inputs are not byte-identical: {name}",
        )
    definitions = [read(path / "definition.json") for path in (BASE, root)]
    require(
        definitions[1]["system_prompt"]
        == remove_statement_prompt(definitions[0]["system_prompt"]),
        "Prompt changed beyond two statement removals",
    )
    manifests = [read(path / "extract/manifest.json") for path in (BASE, root)]
    old, new = [manifest["config"] for manifest in manifests]
    require(
        {k: v for k, v in old.items() if k != "definition"}
        == {k: v for k, v in new.items() if k != "definition"},
        "Unplanned model, runtime or decoding change",
    )
    require(
        set(plan["common_fields"]) == COMMON_KEYS == set(reference["common_fields"]),
        "Common-field contract changed",
    )
    dataset = read(root / "inputs.json")
    for manifest, definition in zip(manifests, definitions, strict=True):
        require(
            manifest["records"] == 15
            and manifest["config_sha256"] == digest(manifest["config"])
            and manifest["input_sha256"] == digest(dataset)
            and manifest["definition_sha256"] == digest(definition)
            and manifest["config"]["definition"] == definition,
            "Manifest identity differs",
        )
        for key, value in plan["generation"].items():
            if key != "gpu_index":
                require(
                    manifest["config"].get(key) == value,
                    f"Generation differs from plan: {key}",
                )
        for name, expected in manifest["config"]["code_sha256"].items():
            require(sha(Path(name)) == expected, "Inference code changed")
    for entry in read(root / "executed-code.json")["files"]:
        require(
            sha(Path(entry["path"])) == entry["sha256"], "Executed helper code changed"
        )
    for item in dataset["records"]:
        require(
            item["input_sha256"]
            == digest({k: v for k, v in item.items() if k != "input_sha256"}),
            "Input fingerprint differs",
        )
    return plan, reference, manifests, indexed(dataset["records"], "inputs")


def arm_timing(summary, rows, manifest):
    require(
        summary["state"] == "completed"
        and summary["processed"] == summary["expected"] == 15
        and summary["config_sha256"] == manifest["config_sha256"],
        "Incomplete inference run",
    )
    total = sum(r["seconds"] for r in rows)
    require(
        abs(total - summary["total_inference_seconds"]) < 0.00001, "Timing sum differs"
    )
    for key in ("input_tokens", "output_tokens"):
        require(
            sum(r[key] for r in rows) == summary[key], f"Token total differs: {key}"
        )
    return {
        "inference_seconds": total,
        "mean_seconds": total / len(rows),
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "load_seconds": sum(s.get("load_seconds", 0) for s in summary["sessions"]),
        "warmup_seconds": sum(s.get("warmup_seconds", 0) for s in summary["sessions"]),
        "runner_wall_seconds": sum(s["wall_seconds"] for s in summary["sessions"]),
        "peak_process_mib": summary.get("nvml_process_peak_mib"),
        "peak_allocated_mib": summary.get("peak_allocated_mib"),
        "peak_reserved_mib": summary.get("peak_reserved_mib"),
        "timing_comparable_full_pass": summary["timing_comparable_full_pass"],
        "measurement_repetitions": summary["measurement_repetitions"],
        "timing_scope": summary["timing_scope"],
        "hardware": summary.get("hardware"),
    }


def build(root):
    plan, reference, manifests, inputs = validate_experiment(root)
    reviewed = read(root / "review.json")
    require(
        reference["model"] == reviewed["model"] == "gpt-6-astra"
        and reviewed["status"] == "complete",
        "Actual completed Astra review required",
    )
    require(
        reviewed["reference_sha256"] == sha(root / "reference.json"),
        "Reference changed after review",
    )
    packets = indexed(read(root / "packets.json")["records"], "packets")
    refs, reviews = (
        indexed(reference["records"], "reference"),
        indexed(reviewed["records"], "review"),
    )
    require(
        set(packets)
        == set(refs)
        == set(reviews)
        == set(inputs)
        == {f"K{i:02}" for i in range(1, 16)},
        "Need the same 15 paired groups",
    )
    require(list(inputs) == list(packets), "Inference input order changed")
    rows, paths, raw_by_arm = [], [], {arm: [] for arm in ARMS}
    started = perf_counter()
    for rid, packet in packets.items():
        files = [path / "extract" / f"result-{rid}.json" for path in (BASE, root)]
        results, judgments = (
            [read(path) for path in files],
            [refs[rid]["baseline"], reviews[rid]],
        )
        verify_quotes(refs[rid], packet)
        mechanical = compare_common_schema(packet, *results)
        arms = {}
        for arm, raw, review, file, manifest in zip(
            ARMS, results, judgments, files, manifests, strict=True
        ):
            require(
                raw["id"] == rid
                and raw["input_sha256"] == inputs[rid]["input_sha256"]
                and raw["config_sha256"] == manifest["config_sha256"]
                and sha(file) == review["result_sha256"],
                f"Reviewed output changed: {rid}/{arm}",
            )
            if raw["status"] == "classified":
                require(
                    parse(raw["raw_output"]) == raw["payload"],
                    "Parsed output differs from model text",
                )
            else:
                require(raw["payload"] is None, "Partial output must not be rescued")
            check_review(review, packet)
            fact = (raw.get("payload") or {}).get("fact")
            if "common_fields_sha256" in review:
                require(
                    common_digest({k: v for k, v in fact.items() if k != "statement"})
                    == review["common_fields_sha256"],
                    "Reviewed common fields changed",
                )
            m = mechanical["arms"][arm]
            strict = (
                m["raw"]["status"] == "common_structurally_valid"
                and not m["raw"]["format_changes"]
            )
            diagnostic = m["diagnostic"]["status"] == "common_structurally_valid"
            require(
                m["diagnostic"]["common_fact"] is None
                or "statement" not in m["diagnostic"]["common_fact"],
                "Statement leaked into common fields",
            )
            arms[arm] = {
                "result_sha256": sha(file),
                "raw_status": raw["status"],
                "raw_fact": fact,
                "review": review,
                "mechanical": m,
                "strict_schema_valid": strict,
                "diagnostic_schema_valid": diagnostic,
                "strict_core_ready": strict and review["structured_common_core_usable"],
                "diagnostic_core_ready": diagnostic
                and review["structured_common_core_usable"],
                "seconds": raw["seconds"],
                "input_tokens": raw["input_tokens"],
                "output_tokens": raw["output_tokens"],
            }
            raw_by_arm[arm].append(raw)
        summary = refs[rid]["baseline"]["summary_statement"]
        require(
            summary["text"] == results[0]["payload"]["fact"]["statement"],
            "Baseline statement diagnostic changed",
        )
        errors = [{e["kind"] for e in review["errors"]} for review in judgments]
        blocking = [
            {e["kind"] for e in review["errors"] if e["blocking"]}
            for review in judgments
        ]
        rows.append(
            {
                "id": rid,
                "article": packet["article"],
                "original_passages": packet["passages"],
                "arms": arms,
                "baseline_statement_diagnostic": summary,
                "error_changes": {
                    "introduced_kinds": sorted(errors[1] - errors[0]),
                    "resolved_kinds": sorted(errors[0] - errors[1]),
                    "persistent_kinds": sorted(errors[0] & errors[1]),
                    "introduced_blocking_kinds": sorted(blocking[1] - blocking[0]),
                    "resolved_blocking_kinds": sorted(blocking[0] - blocking[1]),
                },
                "seconds_delta_without_minus_with": results[1]["seconds"]
                - results[0]["seconds"],
            }
        )
        paths.extend(files)
    script_seconds = perf_counter() - started
    timing = {
        arm: arm_timing(read(path / "extract/summary.json"), raw_by_arm[arm], manifest)
        for arm, path, manifest in zip(ARMS, (BASE, root), manifests, strict=True)
    }
    scores = {arm: summarize(rows, arm) for arm in ARMS}
    require(
        scores["with_summary"]["structured_common_core_usable"]
        == reference["summary"]["baseline_structured_common_core_usable"],
        "Frozen baseline count differs",
    )
    pairs = {
        "common_core": paired(
            rows, lambda r: r["review"]["structured_common_core_usable"]
        ),
        "anchor_focus": paired(rows, lambda r: r["review"]["axes"]["anchor_focus"]),
        "strict_core_ready": paired(rows, lambda r: r["strict_core_ready"]),
        "diagnostic_core_ready": paired(rows, lambda r: r["diagnostic_core_ready"]),
    }
    paths += [
        root / name
        for name in (
            "inputs.json",
            "packets.json",
            "definition.json",
            "plan.json",
            "reference.json",
            "review.json",
            "executed-code.json",
            "extract/manifest.json",
            "extract/summary.json",
        )
    ]
    paths += [
        BASE / name
        for name in ("definition.json", "extract/manifest.json", "extract/summary.json")
    ]
    paths += [
        Path(__file__),
        Path("scripts/summary_ablation_pilot.py"),
        Path("scripts/relation_format_diagnostic.py"),
        Path("apps/curation/core_knowledge_name_resolution.py"),
        Path("apps/curation/core_knowledge_optional_entities.py"),
    ]
    delta = (
        timing["without_summary"]["inference_seconds"]
        - timing["with_summary"]["inference_seconds"]
    )
    return {
        "version": VERSION,
        "policy_version": reference["policy_version"],
        "denominator": 15,
        "state": "research_comparison_not_knowledge_export",
        "scores": scores,
        "paired": pairs,
        "common_core_delta_percentage_points": (
            scores["without_summary"]["structured_common_core_usable"]
            - scores["with_summary"]["structured_common_core_usable"]
        )
        / 15
        * 100,
        "error_changes": {
            kind: [
                {"id": r["id"], "kind": value}
                for r in rows
                for value in r["error_changes"][kind]
            ]
            for kind in (
                "introduced_kinds",
                "resolved_kinds",
                "introduced_blocking_kinds",
                "resolved_blocking_kinds",
            )
        },
        "baseline_statement": {
            "supported": sum(
                r["baseline_statement_diagnostic"]["supported"] for r in rows
            ),
            "conflict_ids": [
                r["id"] for r in rows if r["baseline_statement_diagnostic"]["conflicts"]
            ],
            "interpretation": "Statement support and common-field errors are separate. Conflicts do not establish that statement generation caused those errors.",
        },
        "timing": {
            "arms": timing,
            "delta_seconds_without_minus_with": delta,
            "delta_percent_without_vs_with": delta
            / timing["with_summary"]["inference_seconds"]
            * 100,
            "comparison_script_seconds": script_seconds,
            "new_task_model_calls": 15,
            "reused_baseline_task_model_calls": 15,
            "new_repair_calls": 0,
            "review_timing_included": False,
        },
        "records": rows,
        "validation": {
            "input_bytes_identical": True,
            "only_two_statement_prompt_removals": True,
            "all_other_manifest_configuration_identical": True,
            "common_fields": plan["common_fields"],
            "strict_schema_definition": "common_schema raw status passes and no lossless format_changes; its raw status alone already permits those adapters",
            "diagnostic_scope": "Same source-name alignment and optional-ID deferral on common fields only, without creating a statement; no semantic review changes.",
        },
        "limits": [
            "15 reused development groups from five articles; one run per arm, not independent clinical validation or a general causal estimate.",
            "Each arm may choose a different supported focus; review preserves these diagnostics instead of forcing identical claims.",
            "Nullable axes use their applicable denominator; missing applicability is not failure.",
            "Error-kind changes describe reviewed categories, not proof that every same-kind error is the same error.",
            "Astra is a model reference, not human gold. This comparison does not replace the 013 knowledge export or write production data.",
        ],
        "sources": [{"path": str(path), "sha256": sha(path)} for path in paths],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    target = args.root / "evaluation.json"
    require(not target.exists(), "Evaluation is frozen; refusing to overwrite")
    result = build(args.root)
    freeze(target, result)
    print(
        json.dumps(
            {"scores": result["scores"], "paired": result["paired"]}, ensure_ascii=False
        )
    )
