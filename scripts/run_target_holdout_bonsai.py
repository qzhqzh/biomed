"""Run frozen source-binding inputs on a separate GPU with cooperative yielding."""

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
from pathlib import Path

from apps.curation.decision import digest
from scripts.benchmark_bonsai_relations import MODEL, request
from scripts.benchmark_bonsai_stage import predict
from scripts.benchmark_knowledge_candidates import (
    completed_results,
    load_inputs,
    read_json,
    summarize,
)
from scripts.classify_literature import atomic_json, now

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = Path("/appdata/02_resource/apps/biomed/research/runtime-bonsai/build-sm75/bin/llama-server")
CUDA_LIB = Path("/appdata/02_resource/apps/biomed/research/runtime-bonsai/cuda-12.4/lib")
GPU = "GPU-215e6368-b513-b68f-9021-88afbfacee75"
PORT = 50293


def gpu_state():
    command = ["nvidia-smi", "-i", GPU, "--query-gpu=memory.free,utilization.gpu", "--format=csv,noheader,nounits"]
    free, utilization = subprocess.check_output(command, text=True, timeout=8).strip().split(",")
    procs = subprocess.check_output(["nvidia-smi", "-i", GPU, "--query-compute-apps=pid,used_memory",
                                     "--format=csv,noheader,nounits"], text=True, timeout=8)
    owners = {}
    for line in procs.splitlines():
        if not line.strip():
            continue
        pid, memory = line.split(",")
        owners[int(pid)] = int(memory)
    return {"free_mib": int(free), "utilization": int(utilization), "owners": owners}


def busy_owners(own_pid):
    result = subprocess.run(["nvidia-smi", "pmon", "-i", GPU, "-s", "u", "-c", "1"],
                            capture_output=True, text=True, timeout=8, check=True)
    busy = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].startswith("#"):
            continue
        try:
            pid = int(fields[1])
            utilization = int(fields[3]) if fields[3] != "-" else 0
        except ValueError:
            continue
        if pid != own_pid and utilization >= 15:
            busy.append(pid)
    return busy


class YieldMonitor:
    def __init__(self, process, initial):
        self.process = process
        self.initial = initial
        self.stop = threading.Event()
        self.reason = None
        self.peak_mib = 0
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def poll(self):
        while not self.stop.wait(3):
            try:
                state = gpu_state()
                own = state["owners"].get(self.process.pid, 0)
                self.peak_mib = max(self.peak_mib, own)
                other = {pid: size for pid, size in state["owners"].items() if pid != self.process.pid}
                extra = sum(other.values()) - sum(self.initial["owners"].values())
                if (set(other) - set(self.initial["owners"]) or extra > 1024 or state["free_mib"] < 6000
                        or busy_owners(self.process.pid)):
                    self.reason = "Another GPU workload became active or memory headroom decreased; yielding."
                    self.process.terminate()
                    return
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.reason = f"Cannot monitor shared GPU ({type(exc).__name__}); yielding."
                self.process.terminate()
                return

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=12)


def run(base, server):
    base, server = Path(base).resolve(), Path(server).resolve()
    dataset, definition = load_inputs(base / "inputs.json"), read_json(base / "definition.json")
    rows = dataset["records"]
    output = base / "extract"
    output.mkdir(exist_ok=True)
    manifest = read_json(MODEL.parent / "download-manifest.json")
    weight = next(row for row in manifest["files"] if row["rfilename"] == MODEL.name)
    if MODEL.stat().st_size != weight["size"]:
        raise ValueError("Model does not match its existing download manifest")
    if not (CUDA_LIB / "libcudart.so.12").is_file():
        raise ValueError("The previously validated local CUDA runtime is unavailable")
    command = [str(server), "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(PORT),
               "-c", "8192", "-np", "1", "-ngl", "99", "-t", "2", "-b", "512", "-ub", "128", "-fa", "off",
               "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
               "--reasoning", "off", "--no-context-shift", "--no-warmup"]
    config = {"version": "target-holdout-bonsai-v1", "model_id": manifest["repo"],
              "revision": manifest["revision"], "model_identity": weight, "model_path": str(MODEL),
              "definition": definition, "gpu_uuid": GPU, "server_command": command,
              "server_sha256": hashlib.sha256(server.read_bytes()).hexdigest(),
              "cuda_lib": str(CUDA_LIB),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "max_input_tokens": 4096, "max_new_tokens": 2048, "temperature": 0, "seed": 0,
              "input_truncation": False, "prompt_cache": False, "training": False}
    config_sha = digest(config)
    manifest_path = output / "manifest.json"
    lock_path = Path("/tmp/biomed-target-holdout-gpu0.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            return {"state": "completed", "processed": len(completed)}
        initial = gpu_state()
        if initial["free_mib"] < 18000 or initial["utilization"] >= 10 or busy_owners(None):
            raise RuntimeError("The other GPU service is active or does not have sufficient memory headroom")
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", PORT))
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=GPU)
        env["LD_LIBRARY_PATH"] = f"{CUDA_LIB}:{server.parent}:" + env.get("LD_LIBRARY_PATH", "")
        summary = read_json(output / "summary.json") if (output / "summary.json").exists() else {}
        session = {"started_at": now(), "resumed_from": len(completed), "initial_gpu": initial}
        summary.setdefault("sessions", []).append(session)
        started = time.perf_counter()
        process = monitor = None

        def save(state, **extra):
            summary.update({**summarize(rows, completed), "state": state, "updated_at": now(),
                            "config_sha256": config_sha, "gpu_process_peak_mib": monitor.peak_mib if monitor else None,
                            "timing_scope": "warm HTTP template/tokenization, inference and validation; load/warmup separate",
                            **extra})
            atomic_json(output / "summary.json", summary)

        try:
            with (output / f"server-{len(summary['sessions'])}.log").open("w") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            monitor = YieldMonitor(process, initial)
            monitor.thread.start()
            endpoint = f"http://127.0.0.1:{PORT}"
            save("loading")
            while True:
                if process.poll() is not None:
                    raise RuntimeError(monitor.reason or "Model server exited; inspect the experiment log")
                try:
                    if request(endpoint, "/health", timeout=2).get("status") == "ok":
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.perf_counter() - started > 300:
                    raise TimeoutError("Model loading exceeded 300 seconds")
                time.sleep(1)
            session["load_seconds"] = round(time.perf_counter() - started, 3)
            atomic_json(output / "server-props.json", request(endpoint, "/props"))
            save("warming_up")
            warmup = predict(endpoint, definition, rows[0], 4096, 2048)
            atomic_json(output / f"warmup-{len(summary['sessions'])}.json", warmup)
            session.update(warmup_seconds=warmup["seconds"], warmup_status=warmup["status"])
            for row in rows[len(completed):]:
                if monitor.reason or (output / "STOP").exists():
                    save("paused", reason=monitor.reason or "STOP requested")
                    return {"state": "paused", "processed": len(completed), "reason": monitor.reason}
                result = {"id": row["id"], "input_sha256": row["input_sha256"], "created_at": now(),
                          "config_sha256": config_sha, "session": len(summary["sessions"]),
                          **predict(endpoint, definition, row, 4096, 2048)}
                atomic_json(output / f"result-{row['id']}.json", result)
                completed[row["id"]] = result
                save("running")
                if len(completed) % 5 == 0 or len(completed) == len(rows):
                    print(json.dumps({"processed": len(completed), "total": len(rows),
                                      "last_status": result["status"]}), flush=True)
            session.update(ended_at=now(), wall_seconds=round(time.perf_counter() - started, 3))
            save("completed")
            return {"state": "completed", "processed": len(completed)}
        except (OSError, RuntimeError, TimeoutError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
            state = "paused" if monitor and monitor.reason else "failed"
            save(state, reason=monitor.reason if monitor and monitor.reason else f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if monitor:
                monitor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--server", default=DEFAULT_SERVER, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.server), ensure_ascii=False), flush=True)
