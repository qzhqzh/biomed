#!/usr/bin/env python3
"""Offline, resumable binary passage screening against frozen source paragraphs.

No reference labels are read. Outputs are experimental candidates, not clinical
evidence. Model files must already exist under /data/models; no downloads occur.
"""

import argparse
from collections import Counter
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from scripts.benchmark_three_class import GpuMonitor
from scripts.classify_literature import atomic_json, now

VERSION = "evidence-passage-screening-v1"
BASE = ROOT / "var/experiments/evidence-passages-20261023"
MODEL_ROOT = Path("/data/models")
INPUT_FIELDS = ("title", "locator", "text", "previous_text", "next_text")
IDENTITY_FIELDS = ("id", "pmid", "version_id", "source_sha256", "parser_version", "ordinal", "input_sha256")
STATUSES = {"classified", "abstained", "invalid_output", "output_truncated", "input_too_long"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def validate_output(text):
    result = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(result, dict) or set(result) != {"relevant"}:
        raise ValueError("Expected only the relevant field")
    value = result["relevant"]
    if value is not None and type(value) is not bool:
        raise ValueError("relevant must be a JSON boolean or null")
    return value


def load_inputs(path, sample_count):
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise ValueError("Frozen inputs must include documents")
    rows = data.get("records")
    if not isinstance(rows, list) or len(rows) != sample_count or sample_count < 1:
        raise ValueError(f"Expected {sample_count} frozen paragraphs")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not set(INPUT_FIELDS + IDENTITY_FIELDS) <= set(row):
            raise ValueError("Missing passage fields")
        identity = row["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity):
            raise ValueError("Unsafe passage ID")
        if identity in seen:
            raise ValueError("Duplicate passage ID")
        seen.add(identity)
        if not re.fullmatch(r"[1-9][0-9]{0,9}", str(row["pmid"])):
            raise ValueError("Invalid PMID")
        if not all(isinstance(row[key], str) for key in ("title", "text", "previous_text", "next_text")):
            raise ValueError("Passage text fields must be strings")
        if not row["text"].strip() or not isinstance(row["locator"], (str, dict)):
            raise ValueError("Missing passage text or invalid locator")
        if not row["version_id"] or not isinstance(row["parser_version"], str) or not row["parser_version"]:
            raise ValueError("Missing source version/parser provenance")
        if type(row["ordinal"]) is not int or row["ordinal"] < 0:
            raise ValueError("Invalid passage ordinal")
        for key in ("source_sha256", "input_sha256"):
            if not isinstance(row[key], str) or not re.fullmatch(r"[a-f0-9]{64}", row[key]):
                raise ValueError(f"Invalid {key}")
        if row["input_sha256"] != digest({key: value for key, value in row.items() if key != "input_sha256"}):
            raise ValueError("Frozen passage input hash mismatch")
    return data


def checked_model(folder, model_id, revision):
    """Validate existing download identity and shard completeness without rehashing GBs."""
    folder = folder.resolve(strict=True)
    if not folder.is_relative_to(MODEL_ROOT.resolve()) or not folder.is_dir():
        raise ValueError("Model must be a directory under /data/models")
    manifest_path = folder / "download-manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("repo") != model_id or manifest.get("revision") != revision:
        raise ValueError("Model ID/revision differs from the local download manifest")
    files = {}
    for item in manifest["files"]:
        name = item["rfilename"]
        if name in ("README.md", "LICENSE"):
            continue
        path = folder / name
        if not path.resolve(strict=True).is_relative_to(MODEL_ROOT.resolve()):
            raise ValueError("Model resource resolves outside /data/models")
        stat = path.stat()
        if stat.st_size != item["size"] or not path.is_file():
            raise ValueError(f"Incomplete model resource: {name}")
        identity = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns, "inode": stat.st_ino}
        if path.suffix == ".safetensors":
            with path.open("rb") as stream:
                prefix = stream.read(8)
                if len(prefix) != 8:
                    raise ValueError(f"Incomplete weight header: {name}")
                header_size = struct.unpack("<Q", prefix)[0]
                if not 2 <= header_size <= min(100 * 1024**2, stat.st_size - 8):
                    raise ValueError(f"Invalid weight header length: {name}")
                header = json.loads(stream.read(header_size), object_pairs_hook=unique_object)
            offsets = [value["data_offsets"] for key, value in header.items() if key != "__metadata__"]
            if not offsets or max(end for _, end in offsets) != stat.st_size - 8 - header_size:
                raise ValueError(f"Incomplete weight body: {name}")
            if any(type(start) is not int or type(end) is not int or not 0 <= start <= end
                   for start, end in offsets):
                raise ValueError(f"Invalid weight offsets: {name}")
            identity["download_declared_sha256"] = item.get("lfs", {}).get("sha256")
            identity["header_sha256"] = digest(header)
        else:
            identity["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        files[name] = identity
    index = read_json(folder / "model.safetensors.index.json")
    shards = set(index["weight_map"].values())
    if not shards or not shards <= set(files) or any(not name.endswith(".safetensors") for name in shards):
        raise ValueError("Missing indexed weight shards")
    config = read_json(folder / "config.json")
    if config.get("model_type") != "qwen3_5":
        raise ValueError("This runner supports only Qwen3.5 conditional generation")
    return {"files": files, "download_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "validation": "download identity + sizes + safetensors headers/body lengths; weight bodies not rehashed"}


def completed_results(output, rows, config_sha256):
    expected = {row["id"]: row for row in rows}
    completed = {}
    for path in output.glob("result-*.json"):
        result = read_json(path)
        identity = result.get("id")
        if identity not in expected or path.name != f"result-{identity}.json":
            raise ValueError("Unexpected result identity")
        row = expected[identity]
        if result.get("config_sha256") != config_sha256 or any(result.get(key) != row[key] for key in IDENTITY_FIELDS):
            raise ValueError("Result provenance does not match frozen inputs/config")
        status = result.get("status")
        value = result.get("relevant")
        if status not in STATUSES or (status == "classified" and type(value) is not bool):
            raise ValueError("Invalid checkpoint classification status")
        if status != "classified" and value is not None:
            raise ValueError("Unresolved checkpoint cannot have a binary label")
        if not isinstance(result.get("raw_output"), str):
            raise ValueError("Checkpoint is missing raw model output")
        if status in ("classified", "abstained") and validate_output(result["raw_output"]) is not value:
            raise ValueError("Checkpoint label disagrees with raw model output")
        if any(type(result.get(key)) is not int or result[key] < 0 for key in ("input_tokens", "output_tokens")):
            raise ValueError("Checkpoint is missing valid token counts")
        if not isinstance(result.get("seconds"), (int, float)) or not math.isfinite(result["seconds"]) or result["seconds"] < 0:
            raise ValueError("Invalid checkpoint timing")
        completed[identity] = result
    if set(completed) != {row["id"] for row in rows[:len(completed)]}:
        raise ValueError("Checkpoint is not a contiguous prefix of the frozen input order")
    return completed


def summarize(rows, completed):
    records = list(completed.values())
    timings = sorted(result["seconds"] for result in records)
    return {"expected": len(rows), "processed": len(records), "training": False,
            "statuses": dict(Counter(result["status"] for result in records)),
            "relevant": sum(result["relevant"] is True for result in records),
            "irrelevant": sum(result["relevant"] is False for result in records),
            "unresolved": sum(result["relevant"] is None for result in records),
            "total_inference_seconds": round(sum(timings), 6),
            "mean_seconds": round(statistics.mean(timings), 6) if timings else None,
            "median_seconds": round(statistics.median(timings), 6) if timings else None,
            "p95_seconds": timings[math.ceil(0.95 * len(timings)) - 1] if timings else None}


def run(args):
    dataset = load_inputs(args.base / "inputs.json", args.sample_count)
    rows = dataset["records"]
    definition = read_json(args.base / "definition.json")
    if not isinstance(definition.get("version"), str) or not isinstance(definition.get("prompt"), str) or not definition["prompt"].strip():
        raise ValueError("Definition requires a frozen version and prompt")
    model_identity = checked_model(args.model, args.model_id, args.revision)
    versions = {name: importlib.metadata.version(name) for name in
                ("torch", "transformers", "accelerate", "tokenizers", "safetensors", "nvidia-ml-py")}
    config = {"version": VERSION, "model_id": args.model_id, "revision": args.revision,
              "model_path": str(args.model.resolve()), "model_identity": model_identity,
              "backend": "qwen35", "dtype": "float16", "attention": "eager", "batch_size": 1,
              "threads": 2, "enable_thinking": False, "do_sample": False,
              "loading": "sequential; HF_DEACTIVATE_ASYNC_LOAD=true", "max_input_tokens": 6144,
              "max_new_tokens": 64, "input_truncation": False, "input_fields": list(INPUT_FIELDS),
              "warmup": "one unscored pass of first frozen input per process",
              "definition": definition, "runtime": versions, "gpu_uuid": args.gpu_uuid,
              "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                              ("scripts/benchmark_passage_screening.py", "scripts/benchmark_three_class.py",
                               "scripts/classify_literature.py", "apps/curation/decision.py")}}
    config_sha256 = digest(config)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if (manifest.get("config_sha256") != config_sha256 or digest(manifest.get("config")) != config_sha256
                or manifest.get("input_sha256") != digest(dataset)
                or read_json(args.output / "inputs.json") != dataset):
            raise ValueError("Frozen model/config/inputs changed; use a new output directory")
    else:
        if list(args.output.glob("result-*.json")):
            raise ValueError("Results without a manifest cannot be reused")
        atomic_json(args.output / "inputs.json", dataset)
        atomic_json(manifest_path, {"config": config, "config_sha256": config_sha256,
                                  "input_sha256": digest(dataset), "definition_sha256": digest(definition),
                                  "records": len(rows), "created_at": now()})
    completed = completed_results(args.output, rows, config_sha256)
    summary_path = args.output / "summary.json"
    summary = read_json(summary_path) if summary_path.exists() else {}
    if len(completed) == len(rows) and summary.get("state") == "completed":
        if summary.get("processed") != len(rows) or summary.get("config_sha256") != config_sha256:
            raise ValueError("Completed summary does not match its result checkpoints")
        print(json.dumps({"state": "reused", "output": str(args.output), "processed": len(completed)}), flush=True)
        return

    def save(state, **extra):
        summary.update({**summarize(rows, completed), "state": state, "updated_at": now(),
                        "config_sha256": config_sha256, "runtime": versions,
                        "percentile_method": "nearest_rank", "measurement_repetitions": 1,
                        "timing_scope": "warm batch=1 tokenization + transfers + inference + decoding/validation; excludes writes/load/warmup; input-too-long checks included and reported separately",
                        "nvml_sampling_seconds": 0.1, **extra})
        atomic_json(summary_path, summary)

    if len(completed) == len(rows):
        save("completed", recovered_completed_checkpoints=True)
        return
    resumed = bool(completed) or bool(summary.get("sessions"))
    save("preparing", timing_resumed=resumed, timing_comparable_full_pass=not resumed)
    if (args.output / "STOP").exists():
        save("paused")
        return
    os.environ.update(HF_HOME="/data/models/.cache/huggingface", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      USE_TF="0", USE_FLAX="0", TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES=args.gpu_uuid,
                      HF_DEACTIVATE_ASYNC_LOAD="true")
    monitor = None
    session = {"started_at": now(), "resumed_from": len(completed)}
    summary.setdefault("sessions", []).append(session)
    try:
        import torch
        from transformers import AutoTokenizer, GenerationConfig, Qwen3_5ForConditionalGeneration

        torch.set_num_threads(2)
        torch.set_num_interop_threads(2)
        torch.manual_seed(0)
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; no CPU fallback permitted")
        monitor = GpuMonitor(args.gpu_uuid)
        hardware = monitor.idle_check()
        monitor.thread.start()
        torch.cuda.set_per_process_memory_fraction(0.94)
        save("loading", hardware=hardware)
        started = time.perf_counter()
        tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
        model = Qwen3_5ForConditionalGeneration.from_pretrained(
            args.model, local_files_only=True, trust_remote_code=False, dtype=torch.float16,
            attn_implementation="eager", device_map={"": "cuda:0"}).eval()
        torch.cuda.synchronize()
        session["load_seconds"] = round(time.perf_counter() - started, 6)
        parameters = sum(parameter.numel() for parameter in model.parameters())
        generation = GenerationConfig(max_new_tokens=64, do_sample=False, pad_token_id=tokenizer.eos_token_id,
                                      eos_token_id=tokenizer.eos_token_id)

        def predict(row):
            torch.cuda.synchronize()
            started = time.perf_counter()
            document = json.dumps({key: row[key] for key in INPUT_FIELDS}, ensure_ascii=False)
            text = tokenizer.apply_chat_template(
                [{"role": "system", "content": definition["prompt"]}, {"role": "user", "content": document}],
                tokenize=False, add_generation_prompt=True, enable_thinking=False)
            inputs = tokenizer(text, return_tensors="pt", truncation=False)
            count = inputs.input_ids.shape[1]
            if count > 6144:
                return {"status": "input_too_long", "relevant": None, "raw_output": "",
                        "input_tokens": count, "output_tokens": 0,
                        "seconds": round(time.perf_counter() - started, 6)}
            with torch.inference_mode():
                output_ids = model.generate(**inputs.to("cuda"), generation_config=generation)
            torch.cuda.synchronize()
            generated = output_ids[0, count:].tolist()
            output = tokenizer.decode(generated, skip_special_tokens=True)
            result = {"status": "output_truncated", "relevant": None, "raw_output": output,
                      "input_tokens": count, "output_tokens": len(generated)}
            if generated and generated[-1] == tokenizer.eos_token_id:
                try:
                    result["relevant"] = validate_output(output)
                    result["status"] = "abstained" if result["relevant"] is None else "classified"
                except (ValueError, TypeError) as exc:
                    result.update(status="invalid_output", error=str(exc))
            result["seconds"] = round(time.perf_counter() - started, 6)
            return result

        save("warming_up", load_seconds=session["load_seconds"], parameter_count=parameters)
        warmup = predict(rows[0])
        session.update(warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
        save("running", warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
        print(json.dumps({"event": "loaded", "model": args.model_id, "load_seconds": session["load_seconds"],
                          "parameters": parameters, "resumed_from": len(completed)}), flush=True)

        def peaks():
            return {"peak_allocated_mib": max(summary.get("peak_allocated_mib", 0), round(torch.cuda.max_memory_allocated() / 1024**2, 2)),
                    "peak_reserved_mib": max(summary.get("peak_reserved_mib", 0), round(torch.cuda.max_memory_reserved() / 1024**2, 2)),
                    "nvml_process_peak_mib": max(summary.get("nvml_process_peak_mib", 0), round(monitor.peak / 1024**2, 2)),
                    "nvml_errors": list(dict.fromkeys(summary.get("nvml_errors", []) + monitor.errors))}

        for row in rows[len(completed):]:
            if (args.output / "STOP").exists():
                session["ended_at"] = now()
                save("paused", **peaks())
                return
            result = {**{key: row[key] for key in IDENTITY_FIELDS}, "created_at": now(),
                      "config_sha256": config_sha256, "session": len(summary["sessions"]), **predict(row)}
            atomic_json(args.output / f"result-{row['id']}.json", result)
            completed[row["id"]] = result
            save("running", **peaks())
            print(json.dumps({"processed": len(completed), "id": row["id"], "relevant": result["relevant"],
                              "status": result["status"], "seconds": result["seconds"]}), flush=True)
        session["ended_at"] = now()
        save("completed", **peaks())
    except BaseException as exc:
        session["ended_at"] = now()
        save("paused" if isinstance(exc, KeyboardInterrupt) else "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if monitor is not None:
            if monitor.thread.is_alive():
                monitor.close()
            else:
                monitor.nvml.nvmlShutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--sample-count", type=int, default=282)
    parser.add_argument("--gpu-uuid", default="GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2")
    args = parser.parse_args()
    if args.sample_count < 1:
        parser.error("--sample-count must be positive")
    lock_path = ROOT / "var/experiments/gpu-benchmark.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)
