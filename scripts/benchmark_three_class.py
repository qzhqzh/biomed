#!/usr/bin/env python3
"""Sequential, offline, untrained model comparison on a frozen paper set."""

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.three_class import CATEGORIES, load_completed, load_inputs, summarize, validate_output
from scripts.classify_literature import atomic_json, now

BASE = ROOT / "var/experiments/three-class-20261022"
VERSION = "three-class-model-comparison-v1"


def gliner_schema(definition):
    # Exactly the same category definitions, adapted to the specialist's API.
    descriptions = {}
    for category in CATEGORIES:
        descriptions[category] = definition["prompt"].split(category + ": ", 1)[1].split("\n", 1)[0]
    return {"category": {"labels": descriptions, "multi_label": False,
        "prompt": "Classify the actual research subject of this biomedical article. "
        "A specified tumor studied with normal controls or precursor lesions keeps its tumor category. "
        "Mechanism, diagnosis, treatment, risk and survivorship studies qualify when the tumor is substantively studied. "
        "A retraction notice keeps its article topic. Background mentions do not determine the category."}}


class GpuMonitor:
    """NVML process memory sampled separately from PyTorch's exact allocator peaks."""

    def __init__(self, uuid):
        import pynvml
        self.nvml = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByUUID(uuid)
        self.peak = 0
        self.errors = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def idle_check(self):
        info = self.nvml.nvmlDeviceGetMemoryInfo(self.handle)
        processes = self.nvml.nvmlDeviceGetComputeRunningProcesses(self.handle)
        own_bytes = sum(p.usedGpuMemory for p in processes if p.pid == os.getpid())
        # NVML v1 includes ~337 MiB driver reservation on this host, unlike nvidia-smi.
        if any(p.pid != os.getpid() for p in processes) or info.used - own_bytes > 512 * 1024**2:
            raise RuntimeError("Selected GPU is no longer idle; refusing to share it")
        return {"total_mib": info.total / 1024**2, "initial_other_used_mib": (info.used - own_bytes) / 1024**2,
                "name": self.nvml.nvmlDeviceGetName(self.handle),
                "driver": self.nvml.nvmlSystemGetDriverVersion()}

    def poll(self):
        while not self.stop.is_set():
            try:
                for item in self.nvml.nvmlDeviceGetComputeRunningProcesses(self.handle):
                    if item.pid == os.getpid() and isinstance(item.usedGpuMemory, int):
                        self.peak = max(self.peak, item.usedGpuMemory)
            except self.nvml.NVMLError as exc:
                if not self.errors:
                    self.errors.append(str(exc))
            self.stop.wait(0.1)

    def close(self):
        self.stop.set()
        self.thread.join()
        self.nvml.nvmlShutdown()


def run(args):
    rows = load_inputs([args.base / "inputs.json"])
    if len(rows) != args.sample_count:
        raise ValueError(f"Expected {args.sample_count} frozen papers, got {len(rows)}")
    definition = json.loads((args.base / "task-definition-v1.json").read_text())
    schema = gliner_schema(definition)
    versions = {name: importlib.metadata.version(name) for name in (
        "torch", "transformers", "gliner2", "accelerate", "tokenizers", "safetensors", "nvidia-ml-py")}
    config = {"version": VERSION, "model_id": args.model_id, "revision": args.revision,
        "model_path": str(args.model.resolve()), "backend": args.backend, "dtype": "float16",
        "attention": "eager", "batch_size": 1, "threads": 2, "enable_thinking": False,
        "loading": "sequential; HF_DEACTIVATE_ASYNC_LOAD=true",
        "do_sample": False, "max_new_tokens": 128, "max_input_tokens": 6144,
        "warmup": "one unscored pass of first frozen input", "input_truncation": False,
        "definition": definition, "gliner_schema": schema if args.backend == "gliner" else None,
        "gliner_abstention": "none; single-label argmax, no tuned confidence threshold" if args.backend == "gliner" else None,
        "runtime": versions, "gpu_uuid": args.gpu_uuid,
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        # A completed historical run remains reusable after this script gains new
        # dataset support. Its original code/runtime identity is never rewritten.
        previous_config = previous["config"]
        behavior_keys = set(config) - {"code_sha256", "runtime"}
        summary_path = args.output / "summary.json"
        previous_summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        if (previous["config_sha256"] == digest(previous_config)
            and previous["input_sha256"] == digest(rows)
            and all(previous_config.get(key) == config[key] for key in behavior_keys)
            and load_inputs([args.output / "inputs.json"]) == rows
            and previous_summary.get("state") == "completed"
            and previous_summary.get("processed") == len(rows)
            and previous_summary.get("training") is False
            and len(load_completed(args.output, rows, previous["config_sha256"])) == len(rows)):
            print(json.dumps({"state": "reused", "output": str(args.output)}), flush=True)
            return
        if previous["config_sha256"] != digest(config) or previous["input_sha256"] != digest(rows):
            raise ValueError("Frozen run changed; use a new run directory")
    else:
        atomic_json(manifest_path, {"config": config, "config_sha256": digest(config),
            "input_sha256": digest(rows), "records": len(rows), "created_at": now()})
        atomic_json(args.output / "inputs.json", {"records": rows})
    completed = load_completed(args.output, rows, digest(config))
    summary_path = args.output / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    def save(state, **extra):
        summary.update({**summarize(rows, completed), "state": state, "updated_at": now(), **extra})
        atomic_json(summary_path, summary)

    if len(completed) == len(rows):
        print(json.dumps({"state": "reused", "output": str(args.output)}), flush=True)
        return
    if completed:
        raise ValueError("Partial timing run: preserve it and use a new run directory for a comparable full pass")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", USE_TF="0", USE_FLAX="0",
        TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES=args.gpu_uuid,
        HF_DEACTIVATE_ASYNC_LOAD="true")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig, Qwen3_5ForConditionalGeneration

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
    try:
        started = time.perf_counter()
        if args.backend == "gliner":
            from gliner2 import AutoExtractor
            model = AutoExtractor.from_pretrained(str(args.model), local_files_only=True).to("cuda").half().eval()
            tokenizer = model.processor.tokenizer
        else:
            tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
            loader = Qwen3_5ForConditionalGeneration if args.backend == "qwen35" else AutoModelForCausalLM
            model = loader.from_pretrained(args.model, local_files_only=True, trust_remote_code=False,
                dtype=torch.float16, attn_implementation="eager", device_map={"": "cuda:0"}).eval()
        torch.cuda.synchronize()
        load_seconds = time.perf_counter() - started
        parameters = sum(p.numel() for p in model.parameters())
        generation = GenerationConfig(max_new_tokens=128, do_sample=False,
            pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id)

        def predict(row):
            torch.cuda.synchronize()
            started = time.perf_counter()
            document = json.dumps(row["metadata"], ensure_ascii=False)
            if args.backend == "gliner":
                tokens = tokenizer(document, add_special_tokens=True, truncation=False)["input_ids"]
                if len(tokens) > 6144:
                    return {"status": "input_too_long", "category": None, "raw_output": "", "seconds": time.perf_counter() - started}
                with torch.inference_mode():
                    raw = model.classify_text(document, schema, include_confidence=True, max_len=None)
                torch.cuda.synchronize()
                value = raw.get("category")
                category = value.get("label") if isinstance(value, dict) else value
                output = json.dumps({"category": category}, ensure_ascii=False)
                count, generated, stopped = len(tokens), 0, True
            else:
                text = tokenizer.apply_chat_template([
                    {"role": "system", "content": definition["prompt"]},
                    {"role": "user", "content": document}],
                    tokenize=False, add_generation_prompt=True, enable_thinking=False)
                inputs = tokenizer(text, return_tensors="pt", truncation=False)
                count = inputs.input_ids.shape[1]
                if count > 6144:
                    return {"status": "input_too_long", "category": None, "raw_output": "", "seconds": time.perf_counter() - started}
                with torch.inference_mode():
                    output_ids = model.generate(**inputs.to("cuda"), generation_config=generation)
                torch.cuda.synchronize()
                new_ids = output_ids[0, count:].tolist()
                output = tokenizer.decode(new_ids, skip_special_tokens=True)
                raw, generated = output, len(new_ids)
                stopped = bool(new_ids) and new_ids[-1] == tokenizer.eos_token_id
            result = {"status": "output_truncated", "category": None, "raw_output": output,
                "backend_output": raw, "input_tokens": count, "output_tokens": generated}
            if stopped:
                try:
                    result["category"] = validate_output(output)
                    result["status"] = "classified" if result["category"] is not None else "abstained"
                except (ValueError, TypeError) as exc:
                    result.update(status="invalid_output", error=str(exc))
            result["seconds"] = round(time.perf_counter() - started, 6)
            return result

        warmup_start = time.perf_counter()
        predict(rows[0])
        warmup_seconds = time.perf_counter() - warmup_start
        save("running", load_seconds=round(load_seconds, 3), warmup_seconds=round(warmup_seconds, 3),
            parameter_count=parameters)
        print(json.dumps({"event": "loaded", "model": args.model_id, "load_seconds": load_seconds, "parameters": parameters}), flush=True)
        for row in rows:
            if (args.output / "STOP").exists():
                save("paused")
                return
            result = {**{key: row[key] for key in ("pmid", "version_id", "source_sha256", "input_sha256")},
                "created_at": now(), "config_sha256": digest(config), **predict(row)}
            atomic_json(args.output / f"result-{row['pmid']}.json", result)
            completed[row["pmid"]] = result
            save("running", peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 1024**2, 2),
                peak_reserved_mib=round(torch.cuda.max_memory_reserved() / 1024**2, 2),
                nvml_process_peak_mib=round(monitor.peak / 1024**2, 2), nvml_errors=monitor.errors)
            print(json.dumps({"processed": len(completed), "pmid": row["pmid"], "category": result["category"],
                "status": result["status"], "seconds": result["seconds"]}), flush=True)
        timings = sorted(row["seconds"] for row in completed.values())
        save("completed", median_seconds=round(statistics.median(timings), 6),
            p95_seconds=timings[math.ceil(0.95 * len(timings)) - 1],
            percentile_method="nearest_rank",
            timing_scope="warm batch=1 wall time, tokenization + host/device transfers + inference + decoding/validation; excludes file writes",
            nvml_sampling_seconds=0.1, measurement_repetitions=1)
    except Exception as exc:
        save("failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        monitor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--backend", choices=["qwen", "qwen35", "gliner"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=BASE, help="Directory containing frozen inputs and task definition")
    parser.add_argument("--sample-count", type=int, default=30, help="Expected frozen sample count; checked before loading a model")
    parser.add_argument("--gpu-uuid", default="GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2")
    args = parser.parse_args()
    if args.sample_count < 1:
        parser.error("--sample-count must be positive")
    with (ROOT / "var/experiments/gpu-benchmark.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)
