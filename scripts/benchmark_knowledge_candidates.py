#!/usr/bin/env python3
"""Offline, immutable, resumable JSON extraction with the existing Qwen3.5-9B.

Inputs contain preformatted prompts; this runner never reads reference labels or
writes production knowledge. JSON business-schema/source checks run downstream.
Run with /appdata/02_resource/apps/biomed/research/runtime/bin/python.
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
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from scripts.benchmark_passage_screening import checked_model, unique_object
from scripts.benchmark_three_class import GpuMonitor
from scripts.classify_literature import atomic_json, now

VERSION = "knowledge-candidate-json-extraction-v1"
GPU_UUID = "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2"
STATUSES = {"classified", "invalid_json", "output_truncated", "input_too_long"}
CODE_FILES = (
    "scripts/benchmark_knowledge_candidates.py", "scripts/benchmark_passage_screening.py",
    "scripts/benchmark_three_class.py", "scripts/classify_literature.py", "apps/curation/decision.py",
)


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object, parse_constant=reject_constant)


def validate_output(text):
    # Never strip markdown, prose, or trailing material to manufacture validity.
    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Model output must be one JSON object")
    return value


def load_inputs(path):
    dataset = read_json(path)
    if not isinstance(dataset, dict):
        raise ValueError("Inputs must be an object containing records")
    rows = dataset.get("records")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Inputs require at least one frozen record")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not {"id", "input_sha256", "prompt"} <= set(row):
            raise ValueError("Each input requires id, input_sha256, prompt")
        identity = row["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity):
            raise ValueError("Unsafe input ID")
        if identity in seen:
            raise ValueError("Duplicate input ID")
        seen.add(identity)
        if not isinstance(row["prompt"], str) or not row["prompt"].strip():
            raise ValueError("Input prompt must be a non-empty string")
        if row["input_sha256"] != digest({key: value for key, value in row.items() if key != "input_sha256"}):
            raise ValueError("Frozen input hash mismatch")
    return dataset


def completed_results(output, rows, config_sha256):
    expected = {row["id"]: row for row in rows}
    completed = {}
    for path in output.glob("result-*.json"):
        result = read_json(path)
        identity = result.get("id")
        if identity not in expected or path.name != f"result-{identity}.json":
            raise ValueError("Unexpected result identity")
        if result.get("config_sha256") != config_sha256 or result.get("input_sha256") != expected[identity]["input_sha256"]:
            raise ValueError("Checkpoint inputs/config changed")
        status = result.get("status")
        if status not in STATUSES or not isinstance(result.get("raw_output"), str):
            raise ValueError("Invalid checkpoint status/raw output")
        if status == "classified":
            if not isinstance(result.get("payload"), dict) or validate_output(result["raw_output"]) != result["payload"]:
                raise ValueError("Checkpoint payload differs from raw output")
        elif result.get("payload") is not None:
            raise ValueError("Unresolved checkpoint cannot contain a payload")
        if any(type(result.get(key)) is not int or result[key] < 0 for key in ("input_tokens", "output_tokens")):
            raise ValueError("Checkpoint token counts are invalid")
        seconds = result.get("seconds")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Checkpoint timing is invalid")
        completed[identity] = result
    if set(completed) != {row["id"] for row in rows[:len(completed)]}:
        raise ValueError("Checkpoint is not a contiguous prefix of frozen inputs")
    return completed


def summarize(rows, completed):
    results = list(completed.values())
    timings = sorted(result["seconds"] for result in results)
    return {
        "expected": len(rows), "processed": len(results), "training": False,
        "statuses": dict(Counter(result["status"] for result in results)),
        "total_inference_seconds": round(sum(timings), 6),
        "mean_seconds": round(statistics.mean(timings), 6) if timings else None,
        "median_seconds": round(statistics.median(timings), 6) if timings else None,
        "p95_seconds": timings[math.ceil(0.95 * len(timings)) - 1] if timings else None,
        "input_tokens": sum(result["input_tokens"] for result in results),
        "output_tokens": sum(result["output_tokens"] for result in results),
    }


def run(args):
    dataset = load_inputs(args.inputs)
    rows = dataset["records"]
    definition = read_json(args.definition)
    if (not isinstance(definition, dict) or not isinstance(definition.get("version"), str)
            or not definition["version"].strip() or not isinstance(definition.get("system_prompt"), str)
            or not definition["system_prompt"].strip()):
        raise ValueError("Definition requires version and system_prompt")
    model_identity = checked_model(args.model, args.model_id, args.revision)
    versions = {name: importlib.metadata.version(name) for name in
                ("torch", "transformers", "accelerate", "tokenizers", "safetensors", "nvidia-ml-py")}
    config = {
        "version": VERSION, "model_id": args.model_id, "revision": args.revision,
        "model_path": str(args.model.resolve()), "model_identity": model_identity,
        "backend": "qwen35", "dtype": "float16", "attention": "eager", "batch_size": 1,
        "threads": 2, "enable_thinking": False, "do_sample": False, "seed": 0,
        "loading": "sequential; HF_DEACTIVATE_ASYNC_LOAD=true",
        "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens,
        "input_truncation": False, "warmup": "one unscored first-input pass per process",
        "output_contract": "strict JSON object; business schema checked downstream",
        "definition": definition, "runtime": versions, "gpu_uuid": args.gpu_uuid,
        "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES},
    }
    config_sha256 = digest(config)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    existing = manifest_path.exists()
    if existing:
        manifest = read_json(manifest_path)
        if (manifest.get("config_sha256") != config_sha256 or digest(manifest.get("config")) != config_sha256
                or manifest.get("input_sha256") != digest(dataset)
                or read_json(args.output / "inputs.json") != dataset):
            raise ValueError("Frozen model/config/inputs changed; use a new output directory")
    else:
        if list(args.output.glob("result-*.json")) or (args.output / "summary.json").exists():
            raise ValueError("Checkpoints without a manifest cannot be reused")
        atomic_json(args.output / "inputs.json", dataset)
        atomic_json(manifest_path, {"config": config, "config_sha256": config_sha256,
                                  "input_sha256": digest(dataset), "definition_sha256": digest(definition),
                                  "records": len(rows), "created_at": now()})
    completed = completed_results(args.output, rows, config_sha256)
    summary_path = args.output / "summary.json"
    summary = read_json(summary_path) if summary_path.exists() else {}
    if len(completed) == len(rows) and summary.get("state") == "completed":
        if summary.get("processed") != len(rows) or summary.get("config_sha256") != config_sha256:
            raise ValueError("Completed summary differs from checkpoints")
        print(json.dumps({"state": "reused", "output": str(args.output), "processed": len(completed)}), flush=True)
        return
    if existing and not args.resume:
        raise ValueError("Incomplete run exists; use --resume with identical inputs/config")

    def save(state, **extra):
        summary.update({**summarize(rows, completed), "state": state, "updated_at": now(),
                        "config_sha256": config_sha256, "runtime": versions,
                        "percentile_method": "nearest_rank", "measurement_repetitions": 1,
                        "timing_scope": "warm batch=1 tokenization + transfers + inference + decoding/validation; excludes writes/load/warmup; input-too-long checks included",
                        "nvml_sampling_seconds": 0.1, **extra})
        atomic_json(summary_path, summary)

    summary.pop("error", None)
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
    torch = None
    session_started = time.perf_counter()
    session = {"started_at": now(), "resumed_from": len(completed)}
    summary.setdefault("sessions", []).append(session)

    def peaks():
        if monitor is None or torch is None or not torch.cuda.is_initialized():
            return {}
        return {
            "peak_allocated_mib": max(summary.get("peak_allocated_mib", 0), round(torch.cuda.max_memory_allocated() / 1024**2, 2)),
            "peak_reserved_mib": max(summary.get("peak_reserved_mib", 0), round(torch.cuda.max_memory_reserved() / 1024**2, 2)),
            "nvml_process_peak_mib": max(summary.get("nvml_process_peak_mib", 0), round(monitor.peak / 1024**2, 2)),
            "nvml_errors": list(dict.fromkeys(summary.get("nvml_errors", []) + monitor.errors)),
        }

    def end_session():
        session.update(ended_at=now(), wall_seconds=round(time.perf_counter() - session_started, 6))

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
        generation = GenerationConfig(max_new_tokens=args.max_new_tokens, do_sample=False,
                                      pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id)

        def predict(row):
            torch.cuda.synchronize()
            started = time.perf_counter()
            text = tokenizer.apply_chat_template(
                [{"role": "system", "content": definition["system_prompt"]}, {"role": "user", "content": row["prompt"]}],
                tokenize=False, add_generation_prompt=True, enable_thinking=False)
            inputs = tokenizer(text, return_tensors="pt", truncation=False)
            count = inputs.input_ids.shape[1]
            if count > args.max_input_tokens:
                return {"status": "input_too_long", "payload": None, "raw_output": "",
                        "input_tokens": count, "output_tokens": 0,
                        "seconds": round(time.perf_counter() - started, 6)}
            with torch.inference_mode():
                output_ids = model.generate(**inputs.to("cuda"), generation_config=generation)
            torch.cuda.synchronize()
            generated = output_ids[0, count:].tolist()
            output = tokenizer.decode(generated, skip_special_tokens=True)
            result = {"status": "output_truncated", "payload": None, "raw_output": output,
                      "input_tokens": count, "output_tokens": len(generated)}
            if generated and generated[-1] == tokenizer.eos_token_id:
                try:
                    result.update(payload=validate_output(output), status="classified")
                except (ValueError, TypeError) as exc:
                    result.update(status="invalid_json", error=str(exc))
            result["seconds"] = round(time.perf_counter() - started, 6)
            return result

        save("warming_up", load_seconds=session["load_seconds"], parameter_count=parameters)
        warmup = predict(rows[0])
        session.update(warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
        save("running", warmup_seconds=warmup["seconds"], warmup_status=warmup["status"], **peaks())
        print(json.dumps({"event": "loaded", "model": args.model_id, "load_seconds": session["load_seconds"],
                          "parameters": parameters, "resumed_from": len(completed)}), flush=True)
        for row in rows[len(completed):]:
            if (args.output / "STOP").exists():
                end_session()
                save("paused", **peaks())
                return
            result = {"id": row["id"], "input_sha256": row["input_sha256"], "created_at": now(),
                      "config_sha256": config_sha256, "session": len(summary["sessions"]), **predict(row)}
            atomic_json(args.output / f"result-{row['id']}.json", result)
            completed[row["id"]] = result
            save("running", **peaks())
            print(json.dumps({"processed": len(completed), "id": row["id"], "status": result["status"],
                              "seconds": result["seconds"], "output_tokens": result["output_tokens"]}), flush=True)
        end_session()
        save("completed", **peaks())
    except BaseException as exc:
        end_session()
        save("paused" if isinstance(exc, KeyboardInterrupt) else "failed",
             error=f"{type(exc).__name__}: {exc}", **peaks())
        raise
    finally:
        if monitor is not None:
            if monitor.thread.is_alive():
                monitor.close()
            else:
                monitor.nvml.nvmlShutdown()


def self_check():
    assert validate_output(' {"candidate":null,"fields":[{"value":"a"}]} ') == {"candidate": None, "fields": [{"value": "a"}]}
    for value in ('{"a":1,"a":2}', '{"a":{"x":1,"x":2}}', '```json\n{}\n```', '[]', 'null',
                  '{} trailing', '{"x":NaN}', '{"x":Infinity}', '{"x":'):
        try:
            validate_output(value)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"Unexpected accepted output: {value}")
    row = {"id": "sample-1", "prompt": "Extract one candidate."}
    row["input_sha256"] = digest(row)
    with tempfile.TemporaryDirectory(prefix="biomed-candidate-runner-") as folder:
        output = Path(folder)
        atomic_json(output / "inputs.json", {"records": [row]})
        assert load_inputs(output / "inputs.json")["records"] == [row]
        result = {"id": row["id"], "input_sha256": row["input_sha256"], "config_sha256": "c",
                  "status": "classified", "payload": {"candidate": None}, "raw_output": '{"candidate":null}',
                  "seconds": 0.5, "input_tokens": 10, "output_tokens": 5}
        atomic_json(output / "result-sample-1.json", result)
        assert len(completed_results(output, [row], "c")) == 1
        result["payload"] = {"candidate": "tampered"}
        atomic_json(output / "result-sample-1.json", result)
        try:
            completed_results(output, [row], "c")
        except ValueError:
            pass
        else:
            raise AssertionError("Checkpoint payload tamper accepted")
    print(json.dumps({"self_check": "passed", "gpu_loaded": False}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--definition", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model", type=Path, default=Path("/data/models/Qwen3.5-9B"))
    parser.add_argument("--model-id", default="Qwen/Qwen3.5-9B")
    parser.add_argument("--revision", default="c202236235762e1c871ad0ccb60c8ee5ba337b9a")
    parser.add_argument("--gpu-uuid", default=GPU_UUID)
    parser.add_argument("--max-input-tokens", type=int, default=4096)
    parser.add_argument("--max-new-tokens", type=int, default=1536)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        if not all((args.inputs, args.definition, args.output)):
            parser.error("--inputs, --definition and --output are required")
        if args.max_input_tokens < 1 or args.max_new_tokens < 1:
            parser.error("Token limits must be positive")
        if args.gpu_uuid != GPU_UUID:
            parser.error("This experiment is authorized only for GPU3")
        lock_path = ROOT / "var/experiments/gpu-benchmark.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run(args)
