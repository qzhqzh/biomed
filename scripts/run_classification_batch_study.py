"""Compare two five-paper runs with one ten-paper run when shared resources are idle.

All inference is offline and uses the existing GPU3 runner and its per-paper
checkpoints. The study waits for the existing knowledge worker and fusion study;
it never starts or stops either of them and does not write production knowledge.
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.research_history import register_record
from apps.curation.three_class import validate_output
from scripts.benchmark_knowledge_candidates import (
    CODE_FILES,
    completed_results,
    load_inputs,
)
from scripts.classify_literature import atomic_json
from scripts.knowledge_worker import write_status
from scripts.prepare_classification_batch_study import DEFAULT_OUTPUT, file_hash
from scripts.prepare_knowledge_intake import freeze

MODEL_PYTHON = "/appdata/02_resource/apps/biomed/research/runtime/bin/python"
GPU = "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2"
STEPS = (
    ("merged", "inputs-all.json"),
    ("split-1", "inputs-split-1.json"),
    ("split-2", "inputs-split-2.json"),
)
SERVICES = (
    "biomed-knowledge-worker.service",
    "biomed-fusion-study-resume-20261114.service",
)
STOP = False


def read(path):
    return json.loads(Path(path).read_text())


def status(root, state, reason="", **extra):
    write_status(
        Path(root) / "study-status.json",
        {
            "state": state,
            "reason": reason,
            "updated_at": time.time(),
            "gpu": GPU,
            "network_downloads": False,
            "production_writes": False,
            **extra,
        },
    )


def validate_sample(root, *, expected_model=Path("/data/models/Qwen3.5-4B")):
    root = Path(root)
    spec, definition = read(root / "sample.json"), read(root / "definition.json")
    all_data = load_inputs(root / "inputs-all.json")
    parts = [load_inputs(root / name)["records"] for _, name in STEPS[1:]]
    if spec["version"] != "classification-batching-v1" or spec["gpu_uuid"] != GPU:
        raise ValueError("Study version or permitted GPU changed")
    if (
        digest(all_data) != spec["sample_sha256"]
        or digest(definition) != spec["definition_sha256"]
    ):
        raise ValueError("Frozen sample or definition changed")
    if parts[0] + parts[1] != all_data["records"] or list(map(len, parts)) != [5, 5]:
        raise ValueError("Split and merged inputs differ")
    refs = read(root / "reference.json")["records"]
    if digest({"records": refs}) != spec["reference_sha256"]:
        raise ValueError("Frozen reference changed")
    if [row["id"] for row in refs] != [row["id"] for row in all_data["records"]]:
        raise ValueError("Reference identities differ")
    counts = Counter(row.get("category") for row in refs)
    if dict(counts) != {"solid": 4, "hematologic": 3, "non_neoplastic": 3} or any(
        row.get("reference_state") != "determinate" for row in refs
    ):
        raise ValueError("Frozen reference categories differ")
    model = Path(spec["model_path"])
    if model.resolve() != Path(expected_model).resolve():
        raise ValueError("Model must use the agreed Qwen3.5-4B path")
    manifest = read(model / "download-manifest.json")
    if (
        manifest.get("repo") != spec["model_id"]
        or manifest.get("revision") != spec["revision"]
        or spec["max_input_tokens"] != 6144
        or spec["max_new_tokens"] != 128
    ):
        raise ValueError("Frozen model identity or token limits changed")
    return spec, definition, all_data, refs


def verify_study_identity(root):
    """Fail closed if code or the frozen sample changes between steps."""
    paths = tuple(CODE_FILES) + (
        "scripts/run_classification_batch_study.py",
        "scripts/prepare_classification_batch_study.py",
        "apps/curation/three_class.py",
    )
    identity = {
        "sample_spec_sha256": digest(read(Path(root) / "sample.json")),
        "code_sha256": {name: file_hash(ROOT / name) for name in paths},
    }
    freeze(Path(root) / "runner-identity.json", identity)
    return identity


@contextmanager
def study_lock(root):
    """Only one supervisor may update this experiment's checkpoints and metrics."""
    with (Path(root) / "study.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def service_busy(unit):
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode == 0:
        return True
    if result.returncode in (3, 4) and value in ("inactive", "failed", "unknown"):
        return False
    raise RuntimeError("Cannot verify existing service state")


def pressure():
    memory = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if ":" in line and len(line.split()) > 1
    }

    def avg(name, kind, period):
        line = next(
            row
            for row in Path(f"/proc/pressure/{name}").read_text().splitlines()
            if row.startswith(kind + " ")
        )
        return float(dict(item.split("=") for item in line.split()[1:])[period])

    return {
        "available_gib": memory["MemAvailable"] / 1024**2,
        "io_full_10": avg("io", "full", "avg10"),
        "io_full_60": avg("io", "full", "avg60"),
        "memory_some_60": avg("memory", "some", "avg60"),
    }


def start_gate(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not daytime(8, 10) <= now.time().replace(tzinfo=None) < daytime(22, 0):
        return "等待白天安全时段，避开知识处理窗口"
    if any(service_busy(unit) for unit in SERVICES):
        return "现有知识处理或已安排的融合研究运行中"
    p = pressure()
    if p["available_gib"] < 16 or p["io_full_60"] >= 10 or p["memory_some_60"] >= 2:
        return "共享磁盘或内存繁忙，等待资源回落"
    for mount in ("/data", "/appdata"):
        st = os.statvfs(mount)
        if st.f_bavail * st.f_frsize < 10 * 1024**3 or st.f_favail < 4096:
            return "磁盘空间或 inode 低于安全余量"
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "-i",
            GPU,
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    used, utilization = (int(value.strip()) for value in gpu.stdout.strip().split(","))
    processes = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    if (
        used > 512
        or utilization > 20
        or any(
            line.split(",")[0].strip() == GPU
            for line in processes.stdout.splitlines()
            if line.strip()
        )
    ):
        return "指定显卡正在使用，等待空闲"
    return ""


def read_process(pid):
    result = {}
    try:
        values = dict(
            line.split(":", 1)
            for line in Path(f"/proc/{pid}/io").read_text().splitlines()
        )
        result.update(
            read_bytes=int(values["read_bytes"]), write_bytes=int(values["write_bytes"])
        )
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
        hwm = next((line for line in lines if line.startswith("VmHWM:")), None)
        if hwm:
            result["peak_rss_kib"] = int(hwm.split()[1])
    except (OSError, KeyError, ValueError):
        pass
    return result


def update_peak(peak, observed):
    for key, value in observed.items():
        peak[key] = max(peak.get(key, 0), value)


def halt_child(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_step(root, spec, name, inputs):
    root, output = Path(root), Path(root) / name
    output.mkdir(parents=True, exist_ok=True)
    measurement_path = output / "measurements.json"
    measurements = (
        read(measurement_path) if measurement_path.exists() else {"attempts": []}
    )
    if len(measurements["attempts"]) >= 3:
        raise RuntimeError(f"{name} reached three attempts; checkpoints preserved")
    attempt = {"started_at": datetime.now(timezone.utc).isoformat(), "state": "running"}
    measurements["attempts"].append(attempt)
    atomic_json(measurement_path, measurements)
    command = [
        MODEL_PYTHON,
        "-B",
        str(ROOT / "scripts/benchmark_knowledge_candidates.py"),
        "--inputs",
        str(root / inputs),
        "--definition",
        str(root / "definition.json"),
        "--output",
        str(output),
        "--resume",
        "--model",
        spec["model_path"],
        "--model-id",
        spec["model_id"],
        "--revision",
        spec["revision"],
        "--gpu-uuid",
        GPU,
        "--max-input-tokens",
        str(spec["max_input_tokens"]),
        "--max-new-tokens",
        str(spec["max_new_tokens"]),
    ]
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    started = time.monotonic()
    peak, reason, high_io_since, next_guard = {}, "", None, started
    with (output / "runner.log").open("a") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            while True:
                update_peak(peak, read_process(process.pid))
                if process.poll() is not None:
                    break
                now = time.monotonic()
                if STOP:
                    reason = "服务停止，保留逐篇断点"
                elif now >= next_guard:
                    try:
                        p = pressure()
                        if any(service_busy(unit) for unit in SERVICES):
                            reason = "现有后台或融合研究已启动，保留逐篇断点"
                        elif p["available_gib"] < 6 or p["memory_some_60"] >= 5:
                            reason = "主机内存压力过高，保留逐篇断点"
                        elif p["io_full_10"] >= 35:
                            high_io_since = high_io_since or now
                            if now - high_io_since >= 45:
                                reason = "共享磁盘持续繁忙，保留逐篇断点"
                        else:
                            high_io_since = None
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        StopIteration,
                        subprocess.SubprocessError,
                        RuntimeError,
                    ):
                        reason = "无法核验共享资源，保留逐篇断点"
                    next_guard = now + 5
                if reason:
                    halt_child(process)
                    break
                time.sleep(0.5)
        finally:
            if process.poll() is None:
                halt_child(process)
    summary_file = output / "summary.json"
    finished = (
        process.returncode == 0
        and summary_file.exists()
        and read(summary_file).get("state") == "completed"
    )
    if process.returncode == 0 and not finished and not reason:
        reason = "模型运行暂停或未完成，逐篇断点已保留"
    attempt.update(
        state="interrupted" if reason else ("completed" if finished else "failed"),
        reason=reason,
        returncode=process.returncode,
        ended_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3),
        sampled_process_io_and_peak_rss=peak,
        measurement_note="/proc process counters sampled every 0.5 s; last interval can be missed",
    )
    atomic_json(measurement_path, measurements)
    return attempt


def complete_step(root, name, inputs):
    root = Path(root)
    output = root / name
    summary_path = output / "summary.json"
    if not summary_path.exists() or read(summary_path).get("state") != "completed":
        return None
    data, summary, manifest = (
        load_inputs(root / inputs),
        read(summary_path),
        read(output / "manifest.json"),
    )
    if (
        manifest.get("input_sha256") != digest(data)
        or read(output / "inputs.json") != data
        or digest(manifest.get("config")) != manifest.get("config_sha256")
        or manifest.get("definition_sha256") != digest(read(root / "definition.json"))
        or summary.get("config_sha256") != manifest.get("config_sha256")
        or summary.get("processed") != len(data["records"])
    ):
        raise ValueError(f"{name} frozen output identity differs")
    results = completed_results(output, data["records"], manifest["config_sha256"])
    if len(results) != len(data["records"]):
        raise ValueError(f"{name} completed summary lacks a per-paper checkpoint")
    return {
        "summary": summary,
        "manifest": manifest,
        "results": results,
        "measurement": read(output / "measurements.json")
        if (output / "measurements.json").exists()
        else {},
    }


def category(result):
    if result.get("status") != "classified":
        return None
    try:
        value = validate_output(result["raw_output"])
        return value if result.get("payload") == {"category": value} else None
    except (ValueError, TypeError):
        return None


def summarize_method(steps):
    attempts = [
        attempt for step in steps for attempt in step["measurement"].get("attempts", [])
    ]
    complete_timing = (
        len(attempts) == len(steps)
        and all(a.get("state") == "completed" for a in attempts)
        and all(
            "read_bytes" in a.get("sampled_process_io_and_peak_rss", {})
            for a in attempts
        )
    )
    peaks = [a.get("sampled_process_io_and_peak_rss", {}) for a in attempts]
    io_complete = bool(attempts) and all(
        "read_bytes" in p and "write_bytes" in p for p in peaks
    )
    rss_complete = bool(attempts) and all("peak_rss_kib" in p for p in peaks)
    timed = bool(attempts) and all("elapsed_seconds" in a for a in attempts)
    gpu_complete = all(
        isinstance(step["summary"].get("nvml_process_peak_mib"), (int, float))
        and step["summary"]["nvml_process_peak_mib"] > 0
        and not step["summary"].get("nvml_errors")
        for step in steps
    )
    sessions = [
        session for step in steps for session in step["summary"].get("sessions", [])
    ]
    load_complete = bool(sessions) and all(
        isinstance(session.get("load_seconds"), (int, float)) for session in sessions
    )
    inference_complete = all(
        isinstance(step["summary"].get("total_inference_seconds"), (int, float))
        for step in steps
    )
    return {
        "model_loads": len(sessions) if load_complete else None,
        "load_seconds": round(sum(session["load_seconds"] for session in sessions), 3)
        if load_complete
        else None,
        "inference_seconds": round(
            sum(step["summary"]["total_inference_seconds"] for step in steps), 3
        )
        if inference_complete
        else None,
        "wall_seconds": round(sum(a["elapsed_seconds"] for a in attempts), 3)
        if timed
        else None,
        "process_read_bytes_sampled": sum(p["read_bytes"] for p in peaks)
        if io_complete
        else None,
        "process_write_bytes_sampled": sum(p["write_bytes"] for p in peaks)
        if io_complete
        else None,
        "host_peak_rss_kib": max(p["peak_rss_kib"] for p in peaks)
        if rss_complete
        else None,
        "gpu_peak_mib": max(step["summary"]["nvml_process_peak_mib"] for step in steps)
        if gpu_complete
        else None,
        "nvml_errors": [
            e for step in steps for e in step["summary"].get("nvml_errors", [])
        ],
        "comparable_full_pass": complete_timing
        and load_complete
        and gpu_complete
        and rss_complete
        and all(step["summary"].get("timing_comparable_full_pass") for step in steps),
    }


def evaluate(root):
    root = Path(root)
    spec, _, data, refs = validate_sample(root)
    steps = {name: complete_step(root, name, inputs) for name, inputs in STEPS}
    if any(value is None for value in steps.values()):
        raise ValueError("The three study steps are not all complete")
    configs = {v["manifest"]["config_sha256"] for v in steps.values()}
    if len(configs) != 1:
        raise ValueError("The model/config differs between study methods")
    split = {**steps["split-1"]["results"], **steps["split-2"]["results"]}
    merged = steps["merged"]["results"]
    references = {row["id"]: row["category"] for row in refs}
    cases = []
    for row in data["records"]:
        rid = row["id"]
        a, b = category(split[rid]), category(merged[rid])
        cases.append(
            {
                "pmid": rid,
                "reference": references[rid],
                "split": a,
                "merged": b,
                "split_status": split[rid]["status"],
                "merged_status": merged[rid]["status"],
                "same_valid_category": a is not None and a == b,
            }
        )
    return {
        "version": "classification-batching-result-v1",
        "sample_sha256": spec["sample_sha256"],
        "reference_kind": spec["reference_kind"],
        "cases": cases,
        "matches": sum(row["same_valid_category"] for row in cases),
        "count": len(cases),
        "split_reference_matches": sum(
            row["split"] == row["reference"] for row in cases
        ),
        "merged_reference_matches": sum(
            row["merged"] == row["reference"] for row in cases
        ),
        "split": summarize_method([steps["split-1"], steps["split-2"]]),
        "merged": summarize_method([steps["merged"]]),
        "config_sha256": next(iter(configs)),
        "limitations": [
            "同一显卡按合批、拆批顺序运行；页缓存未清空，物理读数受顺序和共享负载影响。",
            "磁盘读写为进程 /proc 计数的采样值，末次采样后短区间可能漏计。",
            "10 篇为分层开发样本，Astra 是模型参考，不能推断全库准确率。",
        ],
    }


def publish(root, report):
    root = Path(root)
    atomic_json(root / "comparison.json", report)
    methods = []
    for key, label in (
        ("split", "两次加载，各处理 5 篇"),
        ("merged", "一次加载，连续处理 10 篇"),
    ):
        values = report[key]
        metrics = [
            {
                "label": "与 Astra 参考一致",
                "numerator": report[key + "_reference_matches"],
                "denominator": report["count"],
            },
        ]
        for field, label, unit in (
            ("model_loads", "模型加载次数", "次"),
            ("load_seconds", "模型加载耗时", "秒"),
            ("inference_seconds", "实际推理耗时", "秒"),
        ):
            if values[field] is not None:
                metrics.append({"label": label, "value": values[field], "unit": unit})
        if values["gpu_peak_mib"] is not None:
            metrics.append(
                {
                    "label": "进程显存峰值",
                    "value": round(values["gpu_peak_mib"] / 1024, 3),
                    "unit": "GiB",
                }
            )
        if values["wall_seconds"] is not None:
            metrics.append(
                {"label": "进程总耗时", "value": values["wall_seconds"], "unit": "秒"}
            )
        if values["process_read_bytes_sampled"] is not None:
            metrics.append(
                {
                    "label": "采样磁盘读取",
                    "value": round(values["process_read_bytes_sampled"] / 1024**3, 3),
                    "unit": "GiB",
                }
            )
        if values["host_peak_rss_kib"] is not None:
            metrics.append(
                {
                    "label": "主机内存峰值",
                    "value": round(values["host_peak_rss_kib"] / 1024**2, 3),
                    "unit": "GiB",
                }
            )
        for metric in metrics:
            metric.setdefault("note", "")
            if metric["label"] == "采样磁盘读取":
                metric["note"] = "进程物理读计数定期采样；受页缓存和运行顺序影响。"
            elif metric["label"] == "进程总耗时":
                metric["note"] = "包含进程启动、加载、预热、推理和结果保存。"
            elif metric["label"] == "进程显存峰值":
                metric["note"] = "NVML 采样峰值；并非两种方法显存的总和。"
        methods.append(
            {
                "id": key,
                "label": label,
                "version": report["config_sha256"],
                "metrics": metrics,
                "conclusion": "连续逐篇推理；未改变模型提示词。",
            }
        )
    measured_at = max(
        a["ended_at"]
        for name, _ in STEPS
        for a in read(root / name / "measurements.json")["attempts"]
        if a.get("ended_at")
    )
    date = (
        datetime.fromisoformat(measured_at)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    labels = {
        "solid": "实体瘤",
        "hematologic": "血液肿瘤",
        "non_neoplastic": "非肿瘤",
        None: "未完成",
    }
    case_findings = [
        f"PMID {row['pmid']}：参考 {labels[row['reference']]}；两次五篇 {labels[row['split']]}；一次十篇 {labels[row['merged']]}。"
        for row in report["cases"]
    ]
    incomplete_timing = any(
        not report[key]["comparable_full_pass"] for key in ("split", "merged")
    )
    timing_notes = (
        [
            "有方法发生中断恢复或缺少进程 I/O 采样；保留逐篇结果，资源数据不作为完整单次运行的直接对照。"
        ]
        if incomplete_timing
        else []
    )
    record = {
        "schema_version": "research-history-v1",
        "id": "classification-batching-20261114",
        "title": "文献三分类合批与资源对照",
        "date": date,
        "stage": "classification",
        "status": "evaluated",
        "sample_count": report["count"],
        "summary": f"同一批 {report['count']} 篇：两种方式有效分类一致 {report['matches']}/{report['count']}。",
        "task": {
            "question": "一次加载处理更多文献能否减少加载和磁盘读取，同时保持分类结果？",
            "input": "相同冻结标题、摘要、MeSH、出版类型和提示词。",
            "outputs": ["实体瘤", "血液肿瘤", "非肿瘤"],
            "not_measured": ["全库准确率", "知识抽取质量"],
        },
        "dataset": {
            "label": "10 篇分层开发样本",
            "role": "development",
            "notes": ["4 篇实体瘤、3 篇血液肿瘤、3 篇非肿瘤，按 PMID 固定选取。"],
        },
        "reference": {
            "label": "现有 Astra 参考",
            "status": "model_reference_not_human_gold",
            "note": "模型参考可修订，不是人工金标准。",
        },
        "identity": {
            "task_version": "literature-three-class-v1",
            "sample_versions": [report["sample_sha256"]],
            "method_versions": [report["config_sha256"]],
            "reference_versions": [file_hash(root / "reference.json")],
            "completeness": "frozen",
        },
        "methods": methods,
        "findings": report["limitations"] + timing_notes + case_findings,
        "next_steps": ["仅在结果一致且运行环境允许时评估接入后台；另测中断恢复。"],
        "provenance": [
            {
                "label": "逐篇对照及资源报告",
                "artifact": str(DEFAULT_OUTPUT / "comparison.json"),
                "sha256": file_hash(root / "comparison.json"),
            }
        ],
        "evaluation": {"cases": report["cases"]},
    }
    return register_record(record)


def study(root, *, wait=False):
    root = Path(root)
    spec, _, _, _ = validate_sample(root)
    verify_study_identity(root)
    last_reason, last_notice = None, 0
    for name, inputs in STEPS:
        while complete_step(root, name, inputs) is None:
            if STOP:
                status(root, "paused", "任务停止，逐篇断点保留", step=name)
                return 2
            try:
                reason = start_gate()
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                subprocess.SubprocessError,
                RuntimeError,
            ):
                reason = "无法确认系统资源，等待后重试"
            if reason:
                if reason != last_reason or time.monotonic() - last_notice >= 300:
                    status(root, "waiting", reason, step=name)
                    last_reason, last_notice = reason, time.monotonic()
                if not wait:
                    return 2
                time.sleep(30)
                continue
            verify_study_identity(root)
            validate_sample(root)
            status(root, "running", "", step=name)
            attempt = run_step(root, spec, name, inputs)
            if attempt["state"] == "failed":
                log = (root / name / "runner.log").read_text(errors="replace")[-8000:]
                conflict = (
                    "Selected GPU is no longer idle; refusing to share it" in log
                    or "BlockingIOError: [Errno 11] Resource temporarily unavailable"
                    in log
                )
                attempts = read(root / name / "measurements.json")["attempts"]
                if conflict and len(attempts) < 3 and wait and not STOP:
                    status(
                        root, "waiting", "显卡或共享锁被占用，保留断点后重试", step=name
                    )
                    time.sleep(60)
                    continue
                status(root, "failed", "模型运行失败，输出和逐篇断点保留", step=name)
                return 1
            if attempt["state"] == "interrupted":
                status(root, "waiting", attempt["reason"], step=name)
                if not wait or STOP:
                    return 2
                time.sleep(60)
        last_reason = None
    verify_study_identity(root)
    report = evaluate(root)
    publish(root, report)
    status(root, "completed", "", matches=report["matches"], count=report["count"])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "report"))
    parser.add_argument("--root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    def stop(signum, frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.action == "report":
        print(json.dumps(evaluate(args.root), ensure_ascii=False, indent=2))
        return 0
    try:
        with study_lock(args.root):
            return study(args.root, wait=args.wait)
    except BlockingIOError:
        print("This study already has an active supervisor", file=sys.stderr)
        return 2
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        status(args.root, "failed", f"{type(exc).__name__}: {str(exc)[:250]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
