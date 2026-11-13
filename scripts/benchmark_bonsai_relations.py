"""Run frozen relation inputs on a local Prism llama-server, without output repair."""
# ruff: noqa: E402

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from scripts.benchmark_knowledge_candidates import (
    completed_results, load_inputs, read_json, summarize, validate_output,
)
from scripts.classify_literature import atomic_json, now

GPU = "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2"
BASE = ROOT / "var/experiments/bonsai-relations-20261030"
MODEL = Path("/data/models/Ternary-Bonsai-2-27B/Ternary-Bonsai-2-27B-PQ2_0.gguf")


def request(endpoint, route, data=None, timeout=600):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(endpoint + route, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class Monitor:
    def __init__(self):
        import pynvml
        self.nvml = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByUUID(GPU)
        info = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
        if pynvml.nvmlDeviceGetComputeRunningProcesses(self.handle) or info.used > 512 * 1024**2:
            raise RuntimeError("GPU3 is not idle")
        self.hardware = {"name": pynvml.nvmlDeviceGetName(self.handle),
                         "driver": pynvml.nvmlSystemGetDriverVersion(),
                         "total_mib": info.total / 1024**2, "initial_other_used_mib": info.used / 1024**2}
        self.peak = 0
        self.errors = []
        self.stop = threading.Event()
        self.pid = None
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def poll(self):
        while not self.stop.wait(0.1):
            try:
                for item in self.nvml.nvmlDeviceGetComputeRunningProcesses(self.handle):
                    if item.pid == self.pid:
                        self.peak = max(self.peak, item.usedGpuMemory)
            except self.nvml.NVMLError as exc:
                if str(exc) not in self.errors:
                    self.errors.append(str(exc))

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        self.nvml.nvmlShutdown()


def predict(endpoint, definition, row):
    start = time.perf_counter()
    messages = [{"role": "system", "content": definition["system_prompt"]},
                {"role": "user", "content": row["prompt"]}]
    rendered = request(endpoint, "/apply-template", {"messages": messages,
                       "add_generation_prompt": True, "reasoning_effort": "none",
                       "chat_template_kwargs": {"enable_thinking": False}})["prompt"]
    tokens = request(endpoint, "/tokenize", {"content": rendered, "add_special": True})["tokens"]
    result = {"input_tokens": len(tokens), "output_tokens": 0, "payload": None, "raw_output": "",
              "rendered_prompt_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
              "rendered_prompt": rendered}
    if len(tokens) > 4096:
        result.update(status="input_too_long", seconds=time.perf_counter() - start)
        return result
    response = request(endpoint, "/completion", {
        "prompt": tokens, "n_predict": 2048, "temperature": 0, "seed": 0,
        "cache_prompt": False, "stream": False, "ignore_eos": False,
        "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0,
    })
    raw = response["content"]
    result.update(raw_output=raw, output_tokens=response["tokens_predicted"],
                  server_timings=response.get("timings"), stop_type=response.get("stop_type"),
                  status="output_truncated", server_tokens_evaluated=response.get("tokens_evaluated"))
    if response.get("truncated"):
        raise RuntimeError("Server truncated input/context unexpectedly")
    if response.get("stop_type") != "limit" and not response.get("stopped_limit", False):
        try:
            result.update(payload=validate_output(raw), status="classified")
        except (ValueError, TypeError) as exc:
            result.update(status="invalid_json", error=str(exc))
    result["seconds"] = round(time.perf_counter() - start, 6)
    return result


def run(args):
    dataset, definition = load_inputs(BASE / "inputs.json"), read_json(BASE / "definition.json")
    rows = dataset["records"]
    output = BASE / "extract"
    output.mkdir(exist_ok=True)
    server = args.server.resolve()
    model_manifest = read_json(MODEL.parent / "download-manifest.json")
    weight = next(f for f in model_manifest["files"] if f["rfilename"] == MODEL.name)
    if MODEL.stat().st_size != weight["size"]:
        raise ValueError("Model file size mismatch")
    command = [str(server), "-m", str(MODEL), "--host", "127.0.0.1", "--port", "50193",
               "-c", "8192", "-np", "1", "-ngl", "99", "-t", "2", "-b", "512", "-ub", "128", "-fa", "off",
               "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
               "--reasoning", "off", "--no-context-shift", "--no-warmup"]
    config = {
        "version": "bonsai-relations-gguf-v1", "model_id": model_manifest["repo"],
        "revision": model_manifest["revision"], "model_path": str(MODEL),
        "model_identity": weight, "backend": "PrismML llama.cpp prism-b10828-a153697",
        "dtype": "PQ2_0", "attention": "flash attention disabled", "batch_size": 1, "threads": 2, "enable_thinking": False,
        "do_sample": False, "seed": 0, "max_input_tokens": 4096, "max_new_tokens": 2048,
        "input_truncation": False, "prompt_cache": False, "warmup": "one unscored first-input pass",
        "definition": definition, "gpu_uuid": GPU, "server_command": command,
        "server_sha256": hashlib.sha256(server.read_bytes()).hexdigest(),
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    config_sha = digest(config)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        old = read_json(manifest_path)
        if old["config_sha256"] != config_sha or old["input_sha256"] != digest(dataset):
            raise ValueError("Frozen run configuration changed")
    else:
        atomic_json(manifest_path, {"config": config, "config_sha256": config_sha,
                    "input_sha256": digest(dataset), "definition_sha256": digest(definition),
                    "records": len(rows), "created_at": now()})
        atomic_json(output / "inputs.json", dataset)
    completed = completed_results(output, rows, config_sha)
    if len(completed) == len(rows):
        print("All outputs already exist; inference skipped", flush=True)
        return
    summary = read_json(output / "summary.json") if (output / "summary.json").exists() else {}
    monitor, proc = None, None
    session_started = time.perf_counter()
    session = {"started_at": now(), "resumed_from": len(completed)}
    summary.setdefault("sessions", []).append(session)

    def save(state, **extra):
        summary.update({**summarize(rows, completed), "state": state, "updated_at": now(),
            "config_sha256": config_sha, "runtime": {"llama.cpp": "prism-b10828-a153697"},
            "measurement_repetitions": 1, "nvml_sampling_seconds": 0.1,
            "timing_scope": "warm batch1 HTTP template/tokenization + inference + validation; no prompt cache; excludes load/warmup",
            "timing_resumed": len(summary["sessions"]) > 1,
            "timing_comparable_full_pass": len(summary["sessions"]) == 1,
            "nvml_process_peak_mib": round(monitor.peak / 1024**2, 2) if monitor else None,
            "nvml_errors": monitor.errors if monitor else [], **extra})
        atomic_json(output / "summary.json", summary)

    lock = (BASE / "gpu3.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monitor = Monitor()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 50193))
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=GPU)
        env["LD_LIBRARY_PATH"] = str(server.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
        started = time.perf_counter()
        with (output / f"server-{len(summary['sessions'])}.log").open("w") as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        monitor.pid = proc.pid
        monitor.thread.start()
        endpoint = "http://127.0.0.1:50193"
        save("loading", hardware=monitor.hardware)
        while True:
            if proc.poll() is not None:
                raise RuntimeError("Server exited; inspect its log")
            try:
                if request(endpoint, "/health", timeout=2).get("status") == "ok":
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.perf_counter() - started > 300:
                raise TimeoutError("Model loading exceeded 300 seconds")
            time.sleep(1)
        session["load_seconds"] = round(time.perf_counter() - started, 6)
        atomic_json(output / "server-props.json", request(endpoint, "/props"))
        save("warming_up", load_seconds=session["load_seconds"])
        warmup = predict(endpoint, definition, rows[0])
        atomic_json(output / f"warmup-{len(summary['sessions'])}.json", warmup)
        session.update(warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
        save("running", warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
        for row in rows[len(completed):]:
            if (output / "STOP").exists():
                session.update(ended_at=now(), wall_seconds=time.perf_counter() - session_started)
                save("paused")
                return
            result = {"id": row["id"], "input_sha256": row["input_sha256"],
                      "created_at": now(), "config_sha256": config_sha,
                      "session": len(summary["sessions"]), **predict(endpoint, definition, row)}
            atomic_json(output / f"result-{row['id']}.json", result)
            completed[row["id"]] = result
            save("running")
            print(json.dumps({k: result[k] for k in ("id", "status", "seconds", "output_tokens")}), flush=True)
        session.update(ended_at=now(), wall_seconds=time.perf_counter() - session_started)
        save("completed")
    except BaseException as exc:
        session.update(ended_at=now(), wall_seconds=time.perf_counter() - session_started)
        save("paused" if isinstance(exc, KeyboardInterrupt) else "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if monitor is not None:
            monitor.close()
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path)
    run(parser.parse_args())
