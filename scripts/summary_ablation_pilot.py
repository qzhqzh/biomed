"""Prepare a statement-only ablation and inspect shared fields without inference."""

import argparse
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.core_knowledge_intake import _drug_locations
from apps.curation.core_knowledge_name_resolution import resolve
from apps.curation.core_knowledge_optional_entities import (
    _located,
    defer_optional_entities,
)
from scripts.evaluate_knowledge_candidates import freeze, read, sha, text
from scripts.relation_fact_pilot import FACT_KEYS, RELATIONS
from scripts.relation_format_diagnostic import adapt

VERSION = "summary-ablation-v1"
BASE = Path("var/experiments/core-knowledge-20261029")
DEFAULT_ROOT = Path("var/experiments/summary-ablation-20261029")
COMMON_KEYS = (FACT_KEYS - {"statement"}) | {"drug_names"}
FIELD_LINE = "relation, statement, drug_names, entity_ids, population, treatment, comparator, conditions, results, scope, evidence."


def remove_statement_prompt(prompt):
    lines = prompt.splitlines(keepends=True)
    descriptions = [line for line in lines if line.startswith("- statement: ")]
    if len(descriptions) != 1 or prompt.count(FIELD_LINE) != 1:
        raise ValueError("Baseline statement prompt contract changed")
    return prompt.replace(descriptions[0], "", 1).replace(
        FIELD_LINE, FIELD_LINE.replace("statement, ", "", 1), 1
    )


def _copy_frozen(source, destination):
    data = Path(source).read_bytes()
    destination = Path(destination)
    if destination.exists():
        if destination.read_bytes() != data:
            raise ValueError(f"Immutable artifact differs: {destination}")
        return
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != data:
                raise ValueError(f"Immutable artifact differs: {destination}") from None
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root, reference, baseline=BASE):
    root, baseline, reference = Path(root), Path(baseline), Path(reference)
    packets, inputs = read(baseline / "packets.json"), read(baseline / "inputs.json")
    ids = [p["id"] for p in packets["records"]]
    if (
        len(ids) != 15
        or len(set(ids)) != 15
        or ids != [r["id"] for r in inputs["records"]]
    ):
        raise ValueError("Expected the same ordered 15 baseline packets and inputs")
    prompt = remove_statement_prompt(
        read(baseline / "definition.json")["system_prompt"]
    )
    read(reference)
    result_paths = [baseline / "extract" / f"result-{rid}.json" for rid in ids]
    sources = [
        {"path": str(path), "sha256": sha(path)}
        for path in [
            baseline / "inputs.json",
            baseline / "packets.json",
            baseline / "definition.json",
            baseline / "extract" / "manifest.json",
            reference,
            *result_paths,
        ]
    ]
    config = read(baseline / "extract" / "manifest.json")["config"]
    expected_config = {
        "model_id": "Qwen/Qwen3.5-9B",
        "dtype": "float16",
        "attention": "eager",
        "batch_size": 1,
        "do_sample": False,
        "enable_thinking": False,
        "max_input_tokens": 4096,
        "max_new_tokens": 2048,
    }
    if any(config.get(key) != value for key, value in expected_config.items()):
        raise ValueError(
            "Baseline generation configuration differs from the paired plan"
        )
    root.mkdir(parents=True, exist_ok=True)
    for name in ("inputs.json", "packets.json"):
        _copy_frozen(baseline / name, root / name)
    _copy_frozen(reference, root / "reference.json")
    freeze(root / "definition.json", {"version": VERSION, "system_prompt": prompt})
    freeze(
        root / "plan.json",
        {
            "version": VERSION,
            "sample_count": 15,
            "baseline_root": str(baseline),
            "arms": {
                "with_summary": "Reuse 15 original 012 predictions unchanged.",
                "without_summary": "One scored call per identical input; omit only statement.",
            },
            "prompt_change": "Delete the complete statement instruction line and statement from the field list; every other prompt character and user input stays unchanged.",
            "generation": {
                "model_id": "Qwen/Qwen3.5-9B",
                "model_path": "/data/models/Qwen3.5-9B",
                "revision": config.get("revision"),
                "gpu_index": 3,
                "gpu_uuid": config.get("gpu_uuid"),
                "dtype": "float16",
                "attention": "eager",
                "batch_size": 1,
                "do_sample": False,
                "enable_thinking": False,
                "seed": 0,
                "max_input_tokens": 4096,
                "max_new_tokens": 2048,
                "input_truncation": False,
                "warmup": config.get("warmup"),
            },
            "common_fields": sorted(COMMON_KEYS),
            "evaluation": "Score common structured fields against original passages; never use either summary as evidence or insert a placeholder statement. Evaluate baseline summary utility/errors separately.",
            "diagnostics": "Apply the same case-only name alignment and optional-entity adapter independently to both arms, with raw and diagnostic checks reported separately; these are not new model calls.",
            "limits": [
                "15 paired groups are exploratory, not a general model benchmark.",
                "No new source-quote field, model repair pass, training or production writes.",
                "Clinical correctness remains a separate Astra model reference, not human gold.",
                "No baseline statement or prior output is provided to the without-summary model.",
            ],
            "sources": sources,
        },
    )
    return {"root": str(root), "records": len(ids), "version": VERSION}


def common_schema(packet, result, *, with_summary):
    """Mechanical check only; return shared fields without creating a statement."""
    payload, format_changes = adapt(result.get("payload"))
    issues, diagnostics = [], []
    output = {
        "version": VERSION,
        "issues": issues,
        "diagnostics": diagnostics,
        "format_changes": format_changes,
        "common_fact": None,
        "semantic_verification": "not_established_by_script",
    }

    def error(code, path):
        issues.append({"code": code, "path": path})

    if result.get("status") != "classified":
        error("raw_output_not_classified", "status")
    if not isinstance(payload, dict) or set(payload) != {"fact", "reason"}:
        error("root_shape", "payload")
        return {**output, "status": "invalid_structure"}
    fact = payload["fact"]
    if fact is None:
        if not text(payload["reason"]):
            error("abstention_reason", "reason")
        return {**output, "status": "invalid_structure" if issues else "abstained"}
    expected = COMMON_KEYS | ({"statement"} if with_summary else set())
    if not isinstance(fact, dict) or set(fact) != expected:
        error("fact_shape", "fact")
        return {**output, "status": "invalid_structure"}
    output["common_fact"] = {key: deepcopy(fact[key]) for key in sorted(COMMON_KEYS)}
    if payload["reason"] is not None:
        error("nonempty_reason_with_fact", "reason")
    if not isinstance(fact["relation"], str) or fact["relation"] not in RELATIONS:
        error("relation_enum", "fact.relation")
    if not text(fact["scope"]):
        error("required_text", "fact.scope")
    for field in ("population", "treatment", "comparator"):
        if fact[field] is not None and not text(fact[field]):
            error("nullable_text", f"fact.{field}")
    for field in ("entity_ids", "drug_names", "conditions", "evidence"):
        if not isinstance(fact[field], list) or any(not text(x) for x in fact[field]):
            error("text_list", f"fact.{field}")
    if issues:
        return {**output, "status": "invalid_structure"}
    entities = {e["id"]: e for e in packet["entities"]}
    passages = {p["alias"]: p for p in packet["passages"]}
    source_text = {p["id"]: p["text"] for p in packet["passages"]}
    literal_names = [_drug_locations(packet, name) for name in fact["drug_names"]]
    for name, locations in zip(fact["drug_names"], literal_names, strict=True):
        if not locations:
            error("drug_name_not_literal", f"fact.drug_names:{name}")
    if not fact["entity_ids"] and not (literal_names and all(literal_names)):
        error("empty_entity_ids", "fact.entity_ids")
    for eid in fact["entity_ids"]:
        if eid not in entities:
            error("unknown_entity_id", f"fact.entity_ids:{eid}")
        elif not _located(entities[eid], source_text):
            error("unlocated_upstream_entity", f"fact.entity_ids:{eid}")
    if not fact["evidence"]:
        error("no_actual_evidence", "fact.evidence")
    for alias in fact["evidence"]:
        if alias not in passages:
            error("unknown_passage", f"fact.evidence:{alias}")
    if not any(
        passages.get(alias, {}).get("role") == "anchor" for alias in fact["evidence"]
    ):
        diagnostics.append({"code": "anchor_not_cited", "path": "fact.evidence"})
    if not isinstance(fact["results"], list):
        error("results_array", "fact.results")
    else:
        for index, row in enumerate(fact["results"]):
            path = f"fact.results[{index}]"
            if (
                not isinstance(row, dict)
                or set(row) != {"measure", "group", "value", "passage"}
                or any(not text(v) for v in row.values())
            ):
                error("result_shape", path)
            elif row["passage"] not in passages:
                error("unknown_passage", path + ".passage")
            elif row["passage"] not in fact["evidence"]:
                error("result_passage_not_in_evidence", path + ".passage")
    return {
        **output,
        "status": "invalid_structure" if issues else "common_structurally_valid",
    }


def compare_common_schema(packet, baseline_result, without_result):
    comparison = {"id": packet["id"], "arms": {}}
    for arm, result, with_summary in (
        ("with_summary", baseline_result, True),
        ("without_summary", without_result, False),
    ):
        view = deepcopy(result)
        view["payload"], _ = adapt(view.get("payload"))
        if (
            with_summary
            and isinstance(view.get("payload"), dict)
            and isinstance(view["payload"].get("fact"), dict)
        ):
            # Summary utility is reviewed separately. It must not supply even
            # an indirect entity-mention condition to the common-field check.
            view["payload"]["fact"].pop("statement", None)
        names = resolve(packet, view)
        refs = defer_optional_entities(packet, names["result_view"])
        comparison["arms"][arm] = {
            "raw": common_schema(packet, result, with_summary=with_summary),
            "diagnostic": common_schema(
                packet, refs["result_view"], with_summary=False
            ),
            "diagnostic_input_scope": "common_fields_only",
            "name_changes": names["changes"],
            "unresolved_names": names["unresolved"],
            "entity_changes": refs["changes"],
            "unresolved_entity_refs": refs["unresolved_entity_refs"],
            "kept_entity_blockers": refs["kept_blockers"],
        }
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument(
        "--check", action="store_true", help="Read-only paired schema report to stdout"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--baseline", type=Path, default=BASE)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    if args.prepare:
        if args.reference is None:
            parser.error("--reference is required for --prepare")
        report = prepare(args.root, args.reference, args.baseline)
    else:
        report = {
            "version": VERSION,
            "records": [
                compare_common_schema(
                    packet,
                    read(args.baseline / "extract" / f"result-{packet['id']}.json"),
                    read(args.root / "extract" / f"result-{packet['id']}.json"),
                )
                for packet in read(args.root / "packets.json")["records"]
            ],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
