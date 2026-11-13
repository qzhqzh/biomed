"""Freeze a source-only typed view of existing assembled entities, without inference."""

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.source_entity_types import RULES, VERSION, identity, refine
from scripts.evaluate_knowledge_candidates import freeze, read, sha

BASE = Path("var/experiments/lean-entities-20261029")
CONTEXT = Path("var/experiments/evidence-passages-20261023/inputs.json")


def build(source_inputs, assembled_path, context_path):
    started = perf_counter()
    source_inputs, assembled_path, context_path = map(Path, (source_inputs, assembled_path, context_path))
    packets = read(source_inputs)["records"]
    assembled = read(assembled_path)["records"]
    context = read(context_path)["records"]
    by_id = {row["id"]: row for row in assembled}
    if len(by_id) != len(assembled) or [p["id"] for p in packets] != [p["id"] for p in assembled]:
        raise ValueError("Source and assembled entity groups must have the same distinct ordered IDs")
    context_by_article = {}
    for passage in context:
        context_by_article.setdefault(identity(passage), []).append(passage)
    records = []
    for packet in packets:
        row_start = perf_counter()
        original = by_id[packet["id"]]
        if original["article"] != packet["article"]:
            raise ValueError(f"Assembled article provenance differs: {packet['id']}")
        full_context = context_by_article.get(identity(packet["article"]))
        if not full_context:
            raise ValueError(f"Missing same-article/version/hash context: {packet['id']}")
        full_by_id = {p["id"]: p for p in full_context}
        if len(full_by_id) != len(full_context):
            raise ValueError("Duplicate same-version full-text passage IDs")
        for passage in packet["passages"]:
            source = full_by_id.get(passage["id"])
            if source is None or any(source.get(k) != passage.get(k) for k in (
                "text", "locator", "version_id", "source_sha256", "parser_version",
            )):
                raise ValueError(f"Frozen selected passage differs from full context: {passage['id']}")
        result = refine(packet, original["entities"], full_context)
        result["script_seconds"] = perf_counter() - row_start
        records.append(result)
    paths = [source_inputs, assembled_path, context_path, Path(__file__), Path("apps/curation/source_entity_types.py")]
    total = sum(len(r["entities"]) for r in records)
    changes = [c for r in records for c in r["changes"]]
    return {
        "version": VERSION, "model_calls": 0, "production_write": False,
        "rules": RULES, "records": records,
        "counts": {"groups": len(records), "entities_checked": total, "changed": len(changes),
                   "untouched": total - len(changes), "by_rule": {rule: sum(c["rule_id"] == rule for c in changes) for rule in RULES}},
        "script_seconds": perf_counter() - started,
        "input_paths": {"source_inputs": str(source_inputs), "assembled": str(assembled_path), "context": str(context_path)},
        "source_hashes": {str(path): sha(path) for path in paths},
        "scope": "Independent script-derived type view of 010 outputs. Original entities, links and guards remain frozen; full 005 text is only definition evidence, never new relation-model input.",
        "limits": ["Two bounded syntactic/source rules, not general medical entity typing.",
                   "Unmatched and unlocated entities stay unchanged; no standard ID or DNA/gene inference.",
                   "Clinical correctness and optional downstream adoption require separate evaluation."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-inputs", type=Path, default=BASE / "source-inputs.json")
    parser.add_argument("--assembled", type=Path, default=BASE / "assembled.json")
    parser.add_argument("--context", type=Path, default=CONTEXT)
    args = parser.parse_args()
    target = args.root / "entity-types.json"
    if target.exists():
        existing = read(target)
        requested = {"source_inputs": args.source_inputs, "assembled": args.assembled, "context": args.context}
        if any(Path(existing["input_paths"][key]).resolve() != path.resolve() for key, path in requested.items()):
            parser.exit(1, "Frozen typed view uses different input paths; use a new experiment root.\n")
        if any(sha(Path(path)) != expected for path, expected in existing["source_hashes"].items()):
            parser.exit(1, "Frozen typed view source changed; use a new experiment root.\n")
        print(json.dumps({"status": "reused", "path": str(target), "counts": existing["counts"]}))
    else:
        result = build(args.source_inputs, args.assembled, args.context)
        args.root.mkdir(parents=True, exist_ok=True)
        freeze(target, result)
        print(json.dumps({"status": "created", "path": str(target), "counts": result["counts"]}))
