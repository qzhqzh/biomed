"""Run the common rules/standards/9B identity step for any knowledge bundle."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest as model_digest
from apps.curation.entity_normalization import (
    FIELDS,
    PROMPT,
    VERSION,
    digest,
    normalize_bundle,
)
from apps.curation.light_entity_linking import load_index
from scripts.benchmark_knowledge_candidates import (
    completed_results,
    load_inputs,
    validate_output,
)
from scripts.light_entity_pilot import CHEMBL, ONCOTREE, STANDARDS
from scripts.prepare_knowledge_target_mapping import freeze, read

MODEL_PYTHON = Path("/appdata/02_resource/apps/biomed/research/runtime/bin/python")
MODEL_ID = "Qwen/Qwen3.5-9B"
MODEL_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
MODEL_CACHE = ROOT / "var/experiments/entity-normalization-cache-v1"


def cache_key(public):
    """Candidate order is not a different identity question; membership is."""
    return digest([MODEL_ID, MODEL_REVISION, PROMPT, {**public, "candidates": sorted(public["candidates"], key=lambda c: c["id"])}])


def decode_result(result):
    if result["status"] == "classified":
        parsed = validate_output(result["raw_output"])
        if parsed != result["payload"]:
            raise ValueError("Stored decision differs from raw output")
        return parsed, None
    raw = result.get("raw_output", "").strip()
    if result["status"] == "invalid_json" and raw.startswith("{") and not raw.endswith("}"):
        # A single missing final brace adds no name, value, field or meaning.
        # Incomplete strings/fields still fail; preserve the raw checkpoint.
        parsed = validate_output(raw + "}")
        if set(parsed) == FIELDS:
            return parsed, "appended_final_object_brace"
    raise ValueError("Model output has no complete, recoverable decision")


def checked_cache(entry, key):
    row, result, config = entry["input"], entry["result"], entry["config"]
    decision, repair = decode_result(result)
    if (cache_key(json.loads(row["prompt"])) != key or row["input_sha256"] != model_digest({k: v for k, v in row.items() if k != "input_sha256"})
            or result["id"] != row["id"] or result["input_sha256"] != row["input_sha256"]
            or result["config_sha256"] != model_digest(config)
            or config.get("model_id") != MODEL_ID or config.get("revision") != MODEL_REVISION
            or config.get("definition", {}).get("system_prompt") != PROMPT
            or entry.get("decision", decision) != decision or entry.get("repair", repair) != repair):
        raise ValueError("Cached entity decision does not match its source/model contract")
    return {**entry, "decision": decision, "repair": repair}


def read_cache(cache, key):
    path = Path(cache) / f"{key}.json"
    if not path.exists():
        return None
    try:
        with (Path(cache) / "cache.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return checked_cache(read(path), key)
    except (ValueError, KeyError, TypeError):
        return None  # Invalid cache is never evidence and cannot block other terms.


def seed_model_cache(model_root, cache):
    """Reuse valid checkpoints, including a completed prefix after interruption."""
    model_root, cache = Path(model_root), Path(cache)
    if not (model_root / "manifest.json").exists():
        return
    manifest = read(model_root / "manifest.json")
    inputs = load_inputs(model_root / "inputs.json")
    if manifest["input_sha256"] != model_digest(inputs):
        raise ValueError("Model cache source input hash mismatch")
    complete = completed_results(model_root, inputs["records"], manifest["config_sha256"])
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "cache.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for row in inputs["records"]:
            result = complete.get(row["id"])
            if not result or result["status"] not in {"classified", "invalid_json"}:
                continue
            try:
                decode_result(result)
            except ValueError:
                continue
            key = cache_key(json.loads(row["prompt"]))
            entry = checked_cache({"input": row, "result": result, "config": manifest["config"],
                                   "source": str(model_root)}, key)
            if not (cache / f"{key}.json").exists():
                freeze(cache / f"{key}.json", entry)


def infer_rows(rows, output, model_python, model_cache):
    job = output / "model-jobs" / digest(rows)[:20]
    freeze(job / "pending-inputs.json", {"records": rows})
    command = [str(model_python), str(ROOT / "scripts/benchmark_knowledge_candidates.py"),
               "--inputs", str(job / "pending-inputs.json"), "--definition", str(output / "definition.json"),
               "--output", str(job), "--max-input-tokens", "4096", "--max-new-tokens", "768", "--resume"]
    before, error = len(list(job.glob("result-*.json"))), None
    with (output / "model.log").open("a") as log:
        try:
            process = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                     stdout=log, stderr=subprocess.STDOUT, check=False)
            if process.returncode:
                error = f"9B normalization runner exited with code {process.returncode}; see model.log"
        except OSError as exc:
            error = str(exc)
    seed_model_cache(job, model_cache)
    return len(list(job.glob("result-*.json"))) - before, error


def run_normalization(bundle_path, registry_path, output, *, use_model=True, model_python=MODEL_PYTHON,
                      standards=STANDARDS, oncotree=ONCOTREE, chembl=CHEMBL, model_cache=MODEL_CACHE):
    bundle, registry = read(bundle_path), read(registry_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    index = load_index(standards, oncotree=oncotree, chembl=chembl)
    manifest = {"version": VERSION, "bundle_sha256": digest(bundle), "registry_sha256": digest(registry),
                "definition_sha256": digest(PROMPT), "resources": {str(path): digest(read(path)) for path in (standards, oncotree, chembl)}}
    freeze(output / "manifest.json", manifest)
    _, _, preliminary = normalize_bundle(bundle, registry, index)
    tasks = {row["task"]["id"]: row["task"] for row in preliminary["records"] if "task" in row}
    rows = []
    for task in tasks.values():
        public = {key: task[key] for key in ("kind", "label", "contexts")}
        public["candidates"] = [{key: candidate.get(key) for key in
                                 ("id", "label", "name_en", "kind", "namespace", "entity_level", "level", "main_id")}
                                for candidate in task["candidates"]]
        for candidate, original in zip(public["candidates"], task["candidates"], strict=True):
            if original.get("aliases"):
                candidate["aliases"] = original["aliases"]
        row = {"id": task["id"], "prompt": json.dumps(public, ensure_ascii=False)}
        rows.append({**row, "input_sha256": model_digest(row)})
    freeze(output / "inputs.json", {"records": rows})
    freeze(output / "definition.json", {"version": VERSION, "system_prompt": PROMPT})
    for path in (output / "model-jobs").glob("*/manifest.json"):
        seed_model_cache(path.parent, model_cache)
    keys = {row["id"]: cache_key(json.loads(row["prompt"])) for row in rows}
    cached = {key: read_cache(model_cache, key) for key in keys.values()}
    initially_cached = sum(bool(value) for value in cached.values())
    pending = [row for row in rows if not cached[keys[row["id"]]]]
    model_error, new_inferences = None, 0
    if pending and use_model:
        new_inferences, model_error = infer_rows(pending, output, model_python, model_cache)
        cached = {key: read_cache(model_cache, key) for key in keys.values()}

    def decide(item):
        entry = cached.get(keys[item["id"]])
        if not entry:
            if not use_model:
                return None
            raise RuntimeError(model_error or "This entity has no completed, valid model decision")
        return entry["decision"]

    patch, updated, report = normalize_bundle(bundle, registry, index, decide=decide if use_model or any(cached.values()) else None)
    # One bounded correction for a rejected structured decision, with the
    # exact validation reason. Never loop until an uncertain identity passes.
    corrections, correction_keys, original_keys = [], {}, dict(keys)
    rejected = {(r["kind"], r["label"], r["version_id"]): r for r in report["records"] if r["state"] == "invalid_decision"}
    for row in rows:
        task = tasks[row["id"]]
        rejected_row = rejected.get((task["kind"], task["label"], task["version_id"]))
        entry = cached[keys[row["id"]]]
        if rejected_row and entry:
            public = {**json.loads(row["prompt"]), "previous_decision": entry["decision"],
                      "validation_feedback": rejected_row["reason"] + ". Reconsider the identity using the original source; return UNRESOLVED if the source is insufficient."}
            key = cache_key(public)
            retry = {"id": "entity-correction-" + key[:24], "prompt": json.dumps(public, ensure_ascii=False)}
            corrections.append({**retry, "input_sha256": model_digest(retry)})
            correction_keys[row["id"]] = key
    correction_cached = {key: read_cache(model_cache, key) for key in correction_keys.values()}
    pending_corrections = [row for row in corrections if not correction_cached[cache_key(json.loads(row["prompt"]))]]
    if pending_corrections and use_model:
        count, error = infer_rows(pending_corrections, output, model_python, model_cache)
        new_inferences += count
        model_error = error or model_error
        correction_cached = {key: read_cache(model_cache, key) for key in correction_keys.values()}
    for task_id, key in correction_keys.items():
        if correction_cached[key]:
            keys[task_id] = key
            cached[key] = correction_cached[key]
    if correction_keys:
        patch, updated, report = normalize_bundle(bundle, registry, index, decide=decide)
    if any(cached.values()):
        patch["source"]["model"] = {"model_id": MODEL_ID, "revision": MODEL_REVISION,
                                    "decisions": [{"task_id": task_id, "cache_key": key,
                                                   "initial_cache_key": original_keys[task_id],
                                                   "input_sha256": cached[key]["input"]["input_sha256"],
                                                   "config_sha256": cached[key]["result"]["config_sha256"],
                                                   "format_repair": cached[key]["repair"]}
                                                  for task_id, key in keys.items() if cached[key]]}
        patch.pop("batch_id")
        patch["batch_id"] = "auto-normalize-" + digest(patch)[:32]
        updated["source"]["patch_sha256"] = digest(patch)
    for row in report["records"]:
        row.pop("task", None)
    report.update(model_inference_records=new_inferences, model_error=model_error, scheduled_model_tasks=len(tasks),
                  reused_model_decisions=initially_cached, correction_tasks=len(corrections))
    # A partial inference can later finish in the same checkpoint directory.
    # Results append by content version instead of overwriting earlier output.
    result_root = output / "results" / digest([patch, updated, report])[:20]
    for name, value in (("registry-patch.json", patch), ("registry-snapshot.json", updated), ("report.json", report)):
        freeze(result_root / name, value)
    return {"registry": result_root / "registry-snapshot.json", "patch": result_root / "registry-patch.json",
            "report_path": result_root / "report.json", "report": report}


def import_patch(path):
    """Use the established guarded registry importer, never a target connection."""
    with Path(path).open("rb") as stream:
        result = subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "manage.py",
                                 "import_entity_registry", "--input", "/dev/stdin"],
                                cwd=ROOT, stdin=stream, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True, help="Current biomed registry snapshot; never a live tumor_api lookup")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-python", type=Path, default=MODEL_PYTHON)
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--import-to-local", action="store_true")
    args = parser.parse_args()
    result = run_normalization(args.bundle, args.registry, args.output, use_model=not args.rules_only, model_python=args.model_python)
    if args.import_to_local:
        receipt = import_patch(result["patch"])
        freeze(result["patch"].parent / "import-receipt.json", receipt)
    print(json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in result.items() if key != "report"}
                     | {"counts": result["report"]["counts"], "model_inference_records": result["report"]["model_inference_records"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
