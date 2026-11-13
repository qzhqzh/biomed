"""Compare compact extraction with 006 without relaxing its candidate contract.

All candidates are rebuilt from frozen model output.  Astra judgments are bound
to those rebuilt payloads, not to a cached assembly's success flag.  This is an
offline research evaluator: it neither writes production knowledge nor contacts
tumor_api.
"""

import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.compact_facts import apply_review, assemble, build_units
from apps.curation.decision import digest
from apps.curation.knowledge_candidates import validate_candidate
from scripts.evaluate_knowledge_candidates import (
    VERDICTS,
    checked_result,
    freeze,
    indexes,
    mapping,
    normalized_entities,
    read,
    record_map,
    sha,
    text,
)
from scripts.evaluate_knowledge_candidates import (
    build as build_baseline,
)
from scripts.prepare_compact_facts import source_prompt, verify_prompt

VERSION = "compact-facts-evaluation-v1"
STAGES = ("before", "after")


def nonnegative(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid nonnegative finite timing: {label}")
    return value


def checked_run(base, stage, packets):
    """Validate strict raw JSON, complete checkpoints, identity and timing sums."""
    inputs_path = base / f"{stage}-inputs.json"
    definition_path = base / f"{stage}-definition.json"
    output = base / f"qwen35-9b-{stage}"
    inputs, definition = read(inputs_path), read(definition_path)
    if (
        definition.get("source_inputs_sha256") != sha(base / "source-inputs.json")
        or definition.get("training") is not False
    ):
        raise ValueError(f"{stage}: definition source/training identity changed")
    rows = record_map(inputs.get("records"), f"{stage} inputs")
    if set(rows) != set(packets):
        raise ValueError(f"{stage}: must cover all source packets exactly once")
    for ident, row in rows.items():
        if (
            row.get("source_input_sha256") != packets[ident]["input_sha256"]
            or not text(row.get("prompt"))
            or row.get("input_sha256")
            != digest(
                {key: value for key, value in row.items() if key != "input_sha256"}
            )
        ):
            raise ValueError(f"{stage}: changed prompt/source identity for {ident}")
    manifest, summary = read(output / "manifest.json"), read(output / "summary.json")
    config = manifest.get("config")
    if (
        not isinstance(config, dict)
        or digest(config) != manifest.get("config_sha256")
        or digest(config.get("definition")) != digest(definition)
        or manifest.get("input_sha256") != digest(inputs)
        or manifest.get("definition_sha256") != digest(definition)
        or type(manifest.get("records")) is not int
        or manifest["records"] != len(packets)
        or digest(read(output / "inputs.json")) != digest(inputs)
    ):
        raise ValueError(f"{stage}: frozen manifest/config/inputs/definition mismatch")
    if (
        summary.get("state") != "completed"
        or any(type(summary.get(k)) is not int for k in ("processed", "expected"))
        or summary["processed"] != len(packets)
        or summary["expected"] != len(packets)
        or summary.get("config_sha256") != manifest["config_sha256"]
        or summary.get("training") is not False
    ):
        raise ValueError(f"{stage}: incomplete or mismatched model summary")
    if {p.name for p in output.glob("result-*.json")} != {
        f"result-{ident}.json" for ident in packets
    }:
        raise ValueError(f"{stage}: missing or unexpected result files")
    results = {}
    for ident, row in rows.items():
        result = read(output / f"result-{ident}.json")
        checked_result(result, row, manifest)
        results[ident] = result
    total = round(sum(r["seconds"] for r in results.values()), 6)
    for key, value in {
        "statuses": dict(Counter(r["status"] for r in results.values())),
        "input_tokens": sum(r["input_tokens"] for r in results.values()),
        "output_tokens": sum(r["output_tokens"] for r in results.values()),
    }.items():
        if digest(summary.get(key)) != digest(value):
            raise ValueError(f"{stage}: summary differs from checkpoints: {key}")
    for key, value in {
        "total_inference_seconds": total,
        "mean_seconds": round(total / len(results), 6),
    }.items():
        actual = nonnegative(summary.get(key), f"{stage}.{key}")
        if not math.isclose(actual, value, rel_tol=0, abs_tol=0.00001):
            raise ValueError(f"{stage}: summary differs from checkpoints: {key}")
    return {
        "output": output,
        "inputs": rows,
        "results": results,
        "manifest": manifest,
        "summary": summary,
        "identity": {
            "inputs_sha256": sha(inputs_path),
            "definition_sha256": sha(definition_path),
            "manifest_sha256": sha(output / "manifest.json"),
            "summary_sha256": sha(output / "summary.json"),
        },
    }


def checked_reviews(paths, baseline, packets):
    """Reference remains the original blinded Astra artifact and definition."""
    definition_sha = sha(baseline / "definition.json")
    reference_owners = {}
    for part in (1, 2):
        path = baseline / f"astra-reference-part{part}.json"
        reference = read(path)
        if (
            reference.get("definition_sha256") != definition_sha
            or reference.get("annotator_model") != "gpt-6-astra"
            or reference.get("status") != "model_reference_not_human_gold"
            or reference.get("blinded_to_candidate_outputs") is not True
        ):
            raise ValueError("Baseline reference provenance mismatch")
        for ident, row in record_map(reference.get("records"), "reference").items():
            if (
                ident not in packets
                or ident in reference_owners
                or row.get("input_sha256") != packets[ident]["input_sha256"]
            ):
                raise ValueError("Baseline reference packet mismatch")
            reference_owners[ident] = sha(path)
    if set(reference_owners) != set(packets):
        raise ValueError("Baseline references do not cover source packets")
    reviews = {}
    for path in paths:
        part = read(path)
        if (
            part.get("reviewer_model") != "gpt-6-astra"
            or part.get("state") != "completed"
            or part.get("baseline_definition_sha256") != definition_sha
        ):
            raise ValueError("Astra review incomplete or baseline definition changed")
        rows = record_map(part.get("records"), "Astra review")
        if "expected" in part and (
            type(part["expected"]) is not int or part["expected"] != len(rows)
        ):
            raise ValueError("Astra review coverage mismatch")
        for ident, row in rows.items():
            if (
                ident not in packets
                or ident in reviews
                or row.get("input_sha256") != packets[ident]["input_sha256"]
                or part.get("baseline_reference_sha256") != reference_owners[ident]
            ):
                raise ValueError("Astra review packet/reference identity changed")
            for stage in STAGES:
                review = row.get(stage)
                if (
                    not isinstance(review, dict)
                    or review.get("verdict") not in VERDICTS
                    or not text(review.get("note"))
                    or not isinstance(review.get("errors"), list)
                    or type(review.get("core_statement_supported")) is not bool
                    or not isinstance(review.get("candidate_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", review["candidate_sha256"])
                ):
                    raise ValueError(f"Invalid {stage} Astra review for {ident}")
                if any(
                    not isinstance(error, dict) or not text(error.get("category"))
                    for error in review["errors"]
                ):
                    raise ValueError("Astra error requires a category")
            reviews[ident] = row
    if set(reviews) != set(packets):
        raise ValueError("Astra must review before/after for every packet exactly once")
    return reviews


def metrics(rows):
    entities = [entity for row in rows for entity in row["normalized_entities"]]
    raw_entities = [
        entity
        for row in rows
        if isinstance(row["candidate"], dict)
        and isinstance(row["candidate"].get("entities"), list)
        for entity in row["candidate"]["entities"]
    ]
    compact_entity_count = sum(
        len(row["raw_compact"]["fact"]["entities"])
        for row in rows
        if isinstance(row["raw_compact"], dict)
        and isinstance(row["raw_compact"].get("fact"), dict)
        and isinstance(row["raw_compact"]["fact"].get("entities"), list)
    )
    return {
        "packets": len(rows),
        "documents": len({row["article"]["pmid"] for row in rows}),
        "schema_and_provenance_pass": sum(
            row["validation"]["status"] == "structurally_valid_candidate"
            for row in rows
        ),
        "semantic_verdicts": dict(Counter(row["review"]["verdict"] for row in rows)),
        "semantic_pass": sum(row["review"]["verdict"] == "supported" for row in rows),
        "joint_pass": sum(row["joint_pass"] for row in rows),
        "core_statement_supported": sum(
            row["review"]["core_statement_supported"] for row in rows
        ),
        "abstentions": sum(row["validation"]["status"] == "abstained" for row in rows),
        "entity_mentions": len(raw_entities),
        "canonical_entity_mentions": len(raw_entities),
        "raw_compact_entity_mentions": compact_entity_count,
        "rawjson_statuses": dict(Counter(row["rawjson_status"] for row in rows)),
        "entity_exact_span_count": sum(
            len(row["validation"]["entity_spans"]) for row in rows
        ),
        "source_anchored_entity_mentions": sum(e["source_anchored"] for e in entities),
        "unique_exact_alias_links": sum(
            e["normalization_status"] == "exact_alias_unique" for e in entities
        ),
        "evidence_quotes": sum(
            len(row["candidate"]["evidence"])
            for row in rows
            if isinstance(row["candidate"], dict)
            and isinstance(row["candidate"].get("evidence"), list)
        ),
        "evidence_exact_span_count": sum(
            len(row["validation"]["evidence_spans"]) for row in rows
        ),
        "validation_error_codes": dict(
            Counter(
                error["code"] for row in rows for error in row["validation"]["errors"]
            )
        ),
        "assembly_error_codes": dict(
            Counter(error["code"] for row in rows for error in row["assembly_errors"])
        ),
        "semantic_error_categories": dict(
            Counter(
                error["category"] for row in rows for error in row["review"]["errors"]
            )
        ),
        "mapping_api_ready": 0,
    }


def build(base, baseline, standards_path=None, reviews=None):
    base, baseline = Path(base), Path(baseline)
    standards_path = Path(standards_path or baseline / "standards-snapshot.json")
    reviews = [
        Path(p)
        for p in (reviews or [base / f"astra-review-part{i}.json" for i in (1, 2)])
    ]
    if (base / "source-inputs.json").read_bytes() != (
        baseline / "inputs.json"
    ).read_bytes():
        raise ValueError("Source inputs must be the exact frozen 006 file")
    inputs = read(base / "source-inputs.json")
    packets = record_map(inputs.get("records"), "source inputs")
    if len(packets) != 15 or any(
        not re.fullmatch(r"K(?:0[1-9]|1[0-5])", ident) for ident in packets
    ):
        raise ValueError("This comparison requires the original 15 packets")
    for packet in packets.values():
        if not validate_candidate(
            packet, {"candidate": None, "reason": "Source preflight."}
        )["valid"]:
            raise ValueError("Source packet provenance invalid")
    unit_data = read(base / "units.json")
    units = record_map(unit_data.get("records"), "units")
    if unit_data.get("source_inputs_sha256") != sha(base / "source-inputs.json") or set(
        units
    ) != set(packets):
        raise ValueError("Unit source/coverage mismatch")
    for ident, row in units.items():
        if row.get("source_input_sha256") != packets[ident]["input_sha256"] or digest(
            row.get("units")
        ) != digest(build_units(packets[ident])):
            raise ValueError(
                f"Units differ from deterministic source splitting: {ident}"
            )
    extraction = checked_run(base, "extract", packets)
    verification = checked_run(base, "verify", packets)
    for stage in ("extract", "verify"):
        if read(base / f"{stage}-definition.json").get(
            "baseline_definition_sha256"
        ) != sha(baseline / "definition.json"):
            raise ValueError(
                "New definition must refer to the original baseline definition"
            )
    baseline_evaluation = read(baseline / "evaluation.json")
    # Recompute the old evaluation using its unchanged contract and raw artifacts.
    baseline_rebuilt = build_baseline(
        baseline,
        baseline / "qwen35-9b-joint",
        baseline / "standards-snapshot.json",
        [baseline / f"astra-review-part{i}.json" for i in (1, 2)],
    )
    if digest(baseline_evaluation) != digest(baseline_rebuilt):
        raise ValueError("Baseline evaluation differs from original raw artifacts")
    old_config = read(baseline / "qwen35-9b-joint" / "manifest.json")["config"]
    comparison_config = (
        "model_id",
        "revision",
        "model_identity",
        "backend",
        "dtype",
        "attention",
        "batch_size",
        "threads",
        "enable_thinking",
        "do_sample",
        "seed",
        "gpu_uuid",
    )
    for run in (extraction, verification):
        if any(
            digest(run["manifest"]["config"].get(key)) != digest(old_config.get(key))
            for key in comparison_config
        ):
            raise ValueError(
                "Comparison changed model, precision, GPU or decoding settings"
            )
    timing_data = read(base / "assembly-timing.json")
    timings = record_map(timing_data.get("records"), "assembly timings")
    if set(timings) != set(packets):
        raise ValueError("Assembly timing must cover all 15 packets")
    for ident, row in timings.items():
        for stage in STAGES:
            nonnegative(row.get(f"{stage}_seconds"), f"{ident}.{stage}")
    dictionary_loads = {}
    for stage in STAGES:
        raw_timing = read(base / f"{stage}-timing.json")
        raw_rows = record_map(raw_timing.get("records"), f"{stage} raw assembly timing")
        if set(raw_rows) != set(packets):
            raise ValueError("Incomplete stage assembly timing")
        for ident, row in raw_rows.items():
            if digest(row.get("seconds")) != digest(timings[ident][f"{stage}_seconds"]):
                raise ValueError(
                    "Combined assembly timing differs from original measurement"
                )
        dictionary_loads[stage] = nonnegative(
            raw_timing.get("dictionary_load_seconds"),
            f"{stage}.dictionary_load_seconds",
        )
    review_map = checked_reviews(reviews, baseline, packets)
    if sha(standards_path) != sha(baseline / "standards-snapshot.json"):
        raise ValueError(
            "The comparison must reuse the unchanged baseline standards snapshot"
        )
    index = indexes(read(standards_path))
    stages = {stage: [] for stage in STAGES}
    checks = []
    for ident, packet in packets.items():
        raw_extract = extraction["results"][ident]
        raw_verify = verification["results"][ident]
        compact = raw_extract.get("payload")
        verify_input = verification["inputs"][ident]
        extract_path = extraction["output"] / f"result-{ident}.json"
        if verify_input.get("extract_result_sha256") != sha(
            extract_path
        ) or verify_input.get("compact_sha256") != digest(compact):
            raise ValueError(f"Verification did not bind the extracted facts: {ident}")
        before = assemble(packet, units[ident]["units"], compact)
        if extraction["inputs"][ident]["prompt"] != source_prompt(
            packet, units[ident]["units"]
        ) or verify_input["prompt"] != verify_prompt(
            packet, units[ident]["units"], compact, before
        ):
            raise ValueError(
                f"Prompt contains changed source/draft/script checks: {ident}"
            )
        applied = apply_review(compact, raw_verify.get("payload"))
        if not applied["valid"] and applied.get("compact") is not None:
            raise ValueError(
                "Invalid verification must not yield an accepted candidate"
            )
        after = assemble(packet, units[ident]["units"], applied.get("compact"))
        checks.append(
            {
                "id": ident,
                "decision": applied.get("decision"),
                "valid": applied["valid"],
                "checks": applied.get("checks"),
                "errors": applied["errors"],
                "model_status": raw_verify["status"],
                "model_result_sha256": sha(
                    verification["output"] / f"result-{ident}.json"
                ),
            }
        )
        for stage, assembled in (("before", before), ("after", after)):
            payload = assembled["payload"]
            # Always apply the unchanged v1 contract, independently of assembler flags.
            validation = validate_candidate(packet, payload)
            review = review_map[ident][stage]
            if review["candidate_sha256"] != digest(payload):
                raise ValueError(
                    f"Astra reviewed a different assembled payload: {ident}/{stage}"
                )
            candidate = payload.get("candidate") if isinstance(payload, dict) else None
            normalized = normalized_entities(candidate, validation, index)
            joint = (
                validation["status"] == "structurally_valid_candidate"
                and review["verdict"] == "supported"
            )
            stages[stage].append(
                {
                    "id": ident,
                    "article": packet["article"],
                    "input_sha256": packet["input_sha256"],
                    "payload": payload,
                    "raw_compact": compact
                    if stage == "before"
                    else applied.get("compact"),
                    "rawjson_status": raw_extract["status"]
                    if stage == "before"
                    else raw_verify["status"],
                    "rejected_draft_compact": compact
                    if stage == "after" and not applied["valid"]
                    else None,
                    "candidate": candidate,
                    "candidate_sha256": digest(payload),
                    "validation": validation,
                    "assembly_errors": assembled["errors"],
                    "unit_spans": assembled["unit_spans"],
                    "normalized_entities": normalized,
                    "review": review,
                    "joint_pass": joint,
                    "mapping": mapping(
                        packet, candidate, normalized, validation, review
                    ),
                    "seconds": round(
                        raw_extract["seconds"]
                        + timings[ident]["before_seconds"]
                        + (
                            raw_verify["seconds"] + timings[ident]["after_seconds"]
                            if stage == "after"
                            else 0
                        ),
                        6,
                    ),
                    "extract_result_sha256": sha(extract_path),
                }
            )
    stage_metrics = {stage: metrics(rows) for stage, rows in stages.items()}
    extract_seconds = extraction["summary"]["total_inference_seconds"]
    verify_seconds = verification["summary"]["total_inference_seconds"]
    assembly_seconds = {
        stage: round(sum(row[f"{stage}_seconds"] for row in timings.values()), 6)
        for stage in STAGES
    }
    totals = {
        "before": round(extract_seconds + assembly_seconds["before"], 6),
        "after": round(
            extract_seconds + verify_seconds + sum(assembly_seconds.values()), 6
        ),
    }
    baseline_seconds = baseline_evaluation["timing"]["total_inference_seconds"]
    baseline_joint = baseline_evaluation["metrics"]["joint_pass"]
    return {
        "version": VERSION,
        "stages": {
            stage: {"metrics": stage_metrics[stage], "records": stages[stage]}
            for stage in STAGES
        },
        "targeted_review": {
            "records": checks,
            "decisions": dict(
                Counter(
                    row["decision"]
                    if row["valid"] and isinstance(row["decision"], str)
                    else "invalid"
                    for row in checks
                )
            ),
            "valid": sum(row["valid"] for row in checks),
            "joint_gained": [
                a["id"]
                for b, a in zip(stages["before"], stages["after"], strict=True)
                if not b["joint_pass"] and a["joint_pass"]
            ],
            "joint_lost": [
                a["id"]
                for b, a in zip(stages["before"], stages["after"], strict=True)
                if b["joint_pass"] and not a["joint_pass"]
            ],
        },
        "timing": {
            "extract": extraction["summary"],
            "verify": verification["summary"],
            "assembly_seconds": assembly_seconds,
            "dictionary_load_seconds": dictionary_loads,
            "pipeline_seconds": totals,
            "pipeline_mean_seconds": {
                stage: round(value / 15, 6) for stage, value in totals.items()
            },
            "scope": "逐条抽取+定向核验+脚本组装/精确字典链接；模型与字典加载、预热、Astra评测及I/O等待独立，不是线上吞吐。",
            "assembly_records": list(timings.values()),
        },
        "comparison": {
            "baseline": {
                "metrics": baseline_evaluation["metrics"],
                "inference_seconds": baseline_seconds,
            },
            "denominator": 15,
            "joint_pass_definition": "原v1结构/出处检查为structurally_valid_candidate且Astra整条supported；弃答及失败保留在15条分母。",
            "joint_pass_delta": {
                stage: value["joint_pass"] - baseline_joint
                for stage, value in stage_metrics.items()
            },
            "joint_pass_percentage_point_delta": {
                stage: round((value["joint_pass"] - baseline_joint) * 100 / 15, 6)
                for stage, value in stage_metrics.items()
            },
            "seconds_delta": {
                stage: round(value - baseline_seconds, 6)
                for stage, value in totals.items()
            },
            "speedup": {
                stage: round(baseline_seconds / value, 6) if value else None
                for stage, value in totals.items()
            },
        },
        "sources": {
            "base": str(base.resolve()),
            "baseline": str(baseline.resolve()),
            "standards": str(standards_path.resolve()),
            "reviews": [str(path.resolve()) for path in reviews],
        },
        "identity": {
            "source_inputs_sha256": sha(base / "source-inputs.json"),
            "units_sha256": sha(base / "units.json"),
            "extract": extraction["identity"],
            "verify": verification["identity"],
            "assembly_timing_sha256": sha(base / "assembly-timing.json"),
            "stage_timing_sha256": {
                stage: sha(base / f"{stage}-timing.json") for stage in STAGES
            },
            "baseline_evaluation_sha256": sha(baseline / "evaluation.json"),
            "baseline_definition_sha256": sha(baseline / "definition.json"),
            "baseline_reference_sha256": [
                sha(baseline / f"astra-reference-part{i}.json") for i in (1, 2)
            ],
            "target_contract_sha256": sha(baseline / "target-contract-verified.json"),
            "review_sha256": [sha(path) for path in reviews],
            "standards_sha256": sha(standards_path),
            "assembler_sha256": sha(ROOT / "apps/curation/compact_facts.py"),
            "prompt_builder_sha256": sha(ROOT / "scripts/prepare_compact_facts.py"),
            "validator_sha256": sha(ROOT / "apps/curation/knowledge_candidates.py"),
        },
        "limitations": [
            "同15个目的性开发片段组，已用于方案改进；不是独立留出集或全篇/全库召回率。",
            "Astra为冻结模型参考及独立评审，不是人工金标准；9B定向核验是被测流水线的一部分。",
            "采用原v1最终知识契约，脚本只定位组装；不以格式成功或中心陈述支持代替整条通过。",
            "加载和预热独立记录；006曾断点恢复，两轮时间不可解释为持续线上服务吞吐。",
            "目标映射仍是不可提交预览，未调用或修改tumor_api，未微调。",
        ],
    }


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--standards", type=Path)
    parser.add_argument("--reviews", type=Path, nargs="+")
    args = parser.parse_args()
    evaluation = build(args.base, args.baseline, args.standards, args.reviews)
    freeze(args.base / "evaluation.json", evaluation)
    print(
        json.dumps(
            {stage: value["metrics"] for stage, value in evaluation["stages"].items()},
            ensure_ascii=False,
        )
    )
