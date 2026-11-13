"""Supervised single-GPU knowledge processing; no target production writes."""

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.knowledge_processing import (
    DATABASE,
    LIBRARY,
    STATE_ROOT,
    Queue,
    atomic_json,
    batch_report,
    pipeline_method,
    read,
    source_page,
)

GPU = "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2"
DISCOVERY_PAGE_SIZE = 32
DISCOVERY_RETRY_PAGE_SIZE = 8
DISCOVERY_MAX_PAGES = 8
DISCOVERY_LOCK_TIMEOUT = 2
STATUS_UPDATE_SECONDS = 20
DEFAULTS = {"batch_size": 5, "daily_articles": None, "daily_runtime_seconds": None,
            "poll_seconds": 30, "discovery_seconds": 300, "min_free_gib": 10,
            "stall_seconds": 1800,
            "database": str(DATABASE), "library": str(LIBRARY),
            "dictionary": str(ROOT / "var/experiments/target-compatibility-20261101/target-dictionary.json"),
            "mail_config": str(ROOT / "var/private/collection-mail/config.json")}


def write_status(path, report):
    """Atomically publish regenerable status without syncing it as durable data."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(report, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configuration(root):
    path = Path(root) / "config.json"
    cfg = {**DEFAULTS, **(read(path) if path.exists() else {})}
    if cfg.get("clinical_profile") not in (None, "target-clinical-focus-v1"):
        raise ValueError("Invalid clinical profile")
    if type(cfg.get("minimum_source_priority", 0)) is not int or not 0 <= cfg.get("minimum_source_priority", 0) <= 4:
        raise ValueError("Invalid source priority threshold")
    for key, low, high in (("batch_size", 1, 10), ("daily_articles", 1, 100),
                           ("daily_runtime_seconds", 60, 86400), ("poll_seconds", 5, 300),
                           ("discovery_seconds", 60, 3600), ("min_free_gib", 2, 1000),
                           ("stall_seconds", 300, 21600)):
        if key in {"daily_articles", "daily_runtime_seconds"} and cfg[key] is None:
            continue  # JSON null explicitly disables this daily cap; usage is still recorded.
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"Invalid operating budget: {key}")
    return cfg


def process_start(pid):
    try:
        # starttime uniquely disambiguates reused PIDs within the current boot.
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except (OSError, IndexError):
        return None


def resources_ready(cfg):
    pressure = host_pressure()
    if pressure:
        return "waiting_resources", pressure
    for mount in ("/data", "/appdata"):
        stat = os.statvfs(mount)
        if stat.f_bavail * stat.f_frsize < cfg["min_free_gib"] * 1024**3 or stat.f_favail < 4096:
            return "waiting_storage", f"{mount}剩余空间或inode低于运行预算"
    lock = ROOT / "var/experiments/gpu-benchmark.lock"
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "waiting_gpu", "已有biomed模型任务使用显卡，等待其完成"
    result = subprocess.run(["nvidia-smi", "-i", GPU, "--query-gpu=memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=10, check=True)
    free, used = [int(x.strip()) for x in result.stdout.strip().split(",")]
    if free < 21 * 1024 or used > 20:
        return "waiting_gpu", "指定显卡可用显存不足21GiB或正在被其他任务使用"
    return "ready", ""


def host_pressure(proc=Path("/proc")):
    """Wait before loading weights when the shared host is already under pressure."""
    memory = {line.split(":")[0]: int(line.split()[1]) for line in (proc / "meminfo").read_text().splitlines()}
    if memory["MemAvailable"] < 16 * 1024**2:
        return "主机可用内存不足16GiB，等待后再启动模型"
    for name, kind, threshold in (("io", "full", 20), ("memory", "some", 2)):
        row = next(line for line in (proc / "pressure" / name).read_text().splitlines() if line.startswith(kind + " "))
        averages = dict(item.split("=") for item in row.split()[1:])
        if float(averages["avg60"]) >= threshold:
            return "主机磁盘或内存持续繁忙，等待后再启动模型"
    return ""


def progress(folder):
    folder = Path(folder)
    normalization = folder / "entity-normalization-final"
    jobs = sorted((normalization / "model-jobs").glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
    paths = [("entity_normalization", jobs[-1].parent if jobs else normalization),
             ("relations", folder / "stages/relations/extract"),
             ("entities", folder / "stages/entities/extract"),
             ("screening", folder / "screening/qwen35-9b"),
             ("classification", folder / "classification/extract")]
    state_file = folder / "pipeline-state.json"
    state = read(state_file) if state_file.exists() else {}
    result = {"batch": folder.name, "stage": state.get("current_stage") or "preparing"}
    for stage, path in paths:
        if path.exists():
            result["stage"] = stage if stage == "entity_normalization" else state.get("current_stage") or stage
            summary = path / "summary.json"
            if summary.exists():
                value = read(summary)
                result.update(model_state=value.get("state"), completed=value.get("completed", value.get("processed")),
                              total=value.get("total", value.get("expected", value.get("count"))))
            break
    if (folder / "final/summary.json").exists():
        result["stage"] = "reporting"
    return result


class Worker:
    def __init__(self, root=STATE_ROOT):
        self.queue = Queue(root)
        self.root = self.queue.root
        self.stop = False
        self.cfg = configuration(root)
        self.method = pipeline_method(self.cfg.get("clinical_profile"))
        self.active = None

    def publish(self, state, reason=""):
        limits = {k: self.cfg[k] for k in ("batch_size", "daily_articles", "daily_runtime_seconds")}
        report = self.queue.status(state, config=limits,
                                   progress=progress(self.root / "batches" / self.active) if self.active else None)
        report["reason"] = reason
        report["method_id"] = self.method["id"]
        report["source_discovery"] = self.queue.get("source_discovery", {})
        report["source_last_error"] = self.queue.get("source_last_error", {})
        minimum_priority = self.cfg.get("minimum_source_priority", 0)
        report["source_selection"] = {
            "minimum_priority": minimum_priority,
            "eligible_queued": self.queue.db.execute(
                "SELECT count(*) FROM papers WHERE state='queued' AND priority>=?", (minimum_priority,)).fetchone()[0],
            "clinical_profile": self.method.get("clinical_profile"),
        }
        write_status(self.root / "status.json", report)

    def notify(self, key, subject, message):
        notices = self.queue.get("notifications", {})
        if notices.get("notices", {}).get(key, {}).get("sent_at"):
            return
        pending = self.queue.get("pending_notifications", {})
        pending[key] = {"subject": subject, "message": message}
        self.queue.set("pending_notifications", pending)
        self.deliver_notifications()

    def deliver_notifications(self):
        from scripts.watch_literature import notify

        notices = self.queue.get("notifications", {})
        pending = self.queue.get("pending_notifications", {})
        if not pending:
            return
        for key, item in list(pending.items()):
            notify({"mail_config": self.cfg["mail_config"], "collection_id": "knowledge-processing"},
                   notices, key, item["subject"], item["message"] + "\n工作流：http://192.168.124.2:50011/management/workflows/", time.time())
            if notices.get("notices", {}).get(key, {}).get("sent_at"):
                del pending[key]
            self.queue.set("notifications", notices)
            self.queue.set("pending_notifications", pending)

    def recover(self):
        for row in self.queue.db.execute("SELECT id,pid,process_start FROM batches WHERE state='running'").fetchall():
            if row["pid"] and row["process_start"] and process_start(row["pid"]) == row["process_start"]:
                self.publish("waiting_process", "上次子进程仍在运行，等待退出后恢复，避免重复调用模型")
                return False
            self.queue.failure(row["id"], "服务中断，复用原批次断点", interrupted=True)
        return True

    def child(self, command, folder, label):
        logs = folder / "service-logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{label}.log").open("ab") as output:
            process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
                                            "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
            try:
                self.supervise(process, folder, label)
            finally:
                # Also runs when updating the ledger or status fails. Never
                # leave an unsupervised model consuming shared host resources.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)

    def supervise(self, process, folder, label):
        self.queue.process(folder.name, process.pid, process_start(process.pid))
        last = time.monotonic()
        last_change, last_progress = last, progress(folder)
        last_published = None
        interrupted, stop_at = None, None
        stop_is_failure = False
        while True:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            self.queue.charge(folder.name, now - last)
            last = now
            if last_published is None or now - last_published >= STATUS_UPDATE_SECONDS or process.poll() is not None:
                self.publish("running")
                last_published = now
            latest = progress(folder)
            if latest != last_progress:
                last_change, last_progress = now, latest
            stalled = now - last_change >= self.cfg["stall_seconds"] and process.poll() is None
            stall_reason = "阶段长期无进展，终止本服务子进程后有限重试"
            daily_seconds = self.cfg["daily_runtime_seconds"]
            time_exhausted = daily_seconds is not None and self.queue.usage()["seconds"] >= daily_seconds
            reason = ("服务停止，保留断点" if self.stop else
                      "用户暂停，保留断点" if self.queue.get("paused", False) else
                      "达到每日任务时间预算，次日从断点继续" if time_exhausted else
                      stall_reason if stalled else None)
            if reason and not interrupted:
                interrupted, stop_at = reason, now
                stop_is_failure = reason == stall_reason
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            if stop_at is not None and now - stop_at > 30:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is not None:
                if interrupted:
                    if stop_is_failure:
                        raise RuntimeError(interrupted)
                    raise InterruptedError(interrupted)
                if process.returncode:
                    raise RuntimeError(f"{label}退出码{process.returncode}；日志：service-logs/{label}.log")
                return

    def prepare(self, folder, papers):
        selection = {"selection_method": ("knowledge-service molecular clinical source priority; stable PMID order"
                                          if self.method.get("clinical_profile") else
                                          "knowledge-service existing parsed JATS; therapy title first; stable PMID order"),
                     "records": [{"material_id": r["material_id"], "created_at": r["created_at"], "pmid": r["pmid"]} for r in papers]}
        selection_file = folder / "source-selection.json"
        if selection_file.exists() and read(selection_file) != selection:
            raise ValueError("Selected sources changed")
        atomic_json(selection_file, selection)
        sources = folder / "sources"
        self.child([sys.executable, "-B", "scripts/evidence_pilot.py", "freeze", "--database", self.cfg["database"],
                    "--library", self.cfg["library"], "--output", str(sources), "--limit", str(len(papers)),
                    "--selection-file", str(selection_file), "--via-worker"], folder, "sources")
        documents = read(sources / "snapshot.json")["snapshot"]["documents"]
        expected = {r["pmid"]: r for r in papers}
        if len(documents) != len(expected):
            raise ValueError("Incomplete source snapshot")
        for doc in documents:
            planned = expected[doc["pmid"]]
            if doc["fulltext_version_id"] != planned["version_id"]:
                raise ValueError("Source version changed since scheduling; no silent substitution")
            version = next(v for v in doc["versions"] if v["id"] == doc["fulltext_version_id"])
            if version["sha256"] != planned["sha256"] or doc.get("source_issues"):
                raise ValueError("Source content failed verification")
        return sources / "snapshot.json", selection_file

    def execute(self, batch):
        self.active = batch["id"]
        folder = self.root / "batches" / self.active
        folder.mkdir(parents=True, exist_ok=True)
        self.queue.start(self.active)
        try:
            if batch["method"] != self.method["id"]:
                raise ValueError("Frozen extraction method changed; retain batch for explicit version migration")
            method_file = folder / "service-method.json"
            if not method_file.exists():
                atomic_json(method_file, self.method)
            snapshot, selection = self.prepare(folder, self.queue.papers(self.active))
            profile_args = (["--clinical-profile", self.method["clinical_profile"]]
                            if self.method.get("clinical_profile") else [])
            self.child([sys.executable, "-B", "scripts/run_literature_batch.py", "--root", str(folder),
                        "--snapshot", str(snapshot), "--selection-file", str(selection),
                        "--dictionary", self.cfg["dictionary"], "--import-to-local", *profile_args], folder, "pipeline")
            report = batch_report(folder)
            self.queue.complete(self.active, report)
            self.publish("batch_completed")
        except InterruptedError as exc:
            self.queue.failure(self.active, str(exc), interrupted=True)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
            self.queue.failure(self.active, f"{type(exc).__name__}: {str(exc)[:400]}")
        finally:
            self.active = None

    def discover_source(self):
        """Checkpoint short source pages so a temporary failure loses no prior page."""
        cursor = self.queue.get("source_cursor", "")
        completed = 0

        def record_error(exc, phase):
            code = getattr(exc, "sqlite_errorcode", None)
            name = getattr(exc, "sqlite_errorname", None)
            detail = {"at": time.time(), "phase": phase, "reason": type(exc).__name__,
                      "sqlite_errorcode": code if isinstance(code, int) else None,
                      "sqlite_errorname": name if isinstance(name, str) and name.startswith("SQLITE_") else None,
                      "completed_pages": completed}
            self.queue.set("source_last_error", detail)
            return detail

        def transient(exc):
            code = getattr(exc, "sqlite_errorcode", None)
            if isinstance(code, int):
                return (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}
            return isinstance(exc, sqlite3.OperationalError) and str(exc).lower() in {
                "database is locked", "database table is locked", "locked", "interrupted"}

        for _ in range(DISCOVERY_MAX_PAGES):
            if self.stop:
                break
            try:
                rows, next_cursor = source_page(self.cfg["database"], cursor, limit=DISCOVERY_PAGE_SIZE,
                                                lock_timeout=DISCOVERY_LOCK_TIMEOUT)
            except (OSError, sqlite3.Error) as exc:
                detail = record_error(exc, "source_read")
                if not transient(exc) or self.stop:
                    self.queue.set("source_discovery", {"state": "retry_later", **detail})
                    return
                # Keep the already committed pages and retry the same cursor
                # once after a short wait, with a smaller read-only page.
                time.sleep(.5)
                try:
                    rows, next_cursor = source_page(self.cfg["database"], cursor, limit=DISCOVERY_RETRY_PAGE_SIZE,
                                                    lock_timeout=DISCOVERY_LOCK_TIMEOUT)
                except (OSError, sqlite3.Error) as retry_exc:
                    detail = record_error(retry_exc, "source_retry")
                    self.queue.set("source_discovery", {"state": "retry_later", **detail})
                    return
            try:
                self.queue.discover(rows, self.method["id"])
                # If interrupted between these two operations, discovery is
                # idempotent and the same page is safely seen again.
                self.queue.set("source_cursor", next_cursor)
            except (OSError, sqlite3.Error) as exc:
                detail = record_error(exc, "queue_checkpoint")
                self.queue.set("source_discovery", {"state": "retry_later", **detail})
                return
            completed += 1
            cursor = next_cursor
            if not cursor:
                break
        self.queue.set("source_discovery", {"state": "available", "at": time.time(), "completed_pages": completed})

    def tick(self):
        self.cfg = configuration(self.root)
        self.deliver_notifications()
        if not self.recover():
            return
        self.queue.upgrade_pending_method(self.method["id"])
        counts = dict(self.queue.db.execute("SELECT state,count(*) FROM papers GROUP BY state"))
        done = sum(counts.get(state, 0) for state in ("completed", "partial", "excluded", "failed"))
        if done >= 10:
            self.notify("first-ten", "首批自动知识处理已有结果", f"已对{done}篇完成尝试（含排除与失败，非全部提取成功），逐篇结果及待抽查状态见工作流页面。")
        if done and not any(counts.get(state, 0) for state in ("running", "assigned", "queued")):
            self.notify(f"backlog-{self.method['id']}-{done}", "当前全文知识处理队列已完成尝试",
                        f"本服务累计处理{done}篇：完成{counts.get('completed', 0)}、部分完成{counts.get('partial', 0)}、"
                        f"分类排除{counts.get('excluded', 0)}、失败{counts.get('failed', 0)}。服务继续等待新增已解析全文。")
        if self.queue.get("paused", False):
            reason = self.queue.get("pause_reason", "已暂停")
            self.publish("paused", reason)
            if self.queue.get("consecutive_failures", 0) >= 3:
                self.notify("system-failure", "知识处理已异常暂停", reason)
            return
        if time.time() - self.queue.get("last_discovery", 0) >= self.cfg["discovery_seconds"]:
            self.discover_source()
            self.queue.set("last_discovery", time.time())
        used = self.queue.usage()
        daily_seconds = self.cfg["daily_runtime_seconds"]
        if daily_seconds is not None and used["seconds"] >= daily_seconds:
            self.publish("waiting_budget", "今日运行时间预算已用完，服务保留断点，次日自动继续")
            return
        state, reason = resources_ready(self.cfg)
        if state != "ready":
            self.publish(state, reason)
            return
        batch = self.queue.next_batch(self.method["id"], self.cfg["batch_size"],
                                      self.cfg["daily_articles"], self.cfg["daily_runtime_seconds"],
                                      minimum_priority=self.cfg.get("minimum_source_priority", 0))
        if batch:
            self.execute(batch)
        else:
            daily_articles = self.cfg["daily_articles"]
            if daily_articles is not None and used["articles"] >= daily_articles:
                self.publish("waiting_budget", "今日篇数上限已达到，服务保留断点，次日自动继续")
            else:
                pending_retry = self.queue.db.execute("SELECT 1 FROM batches WHERE state IN ('ready','retry') LIMIT 1").fetchone()
                if self.cfg.get("minimum_source_priority", 0) and not pending_retry:
                    self.publish("waiting", "暂无达到分子临床选文门槛的全文；其他资料保留在队列，等待补充合适全文")
                else:
                    self.publish("waiting", "等待下一批、重试时间或新全文；资源空闲时自动继续")

    def run(self, once=False):
        with (self.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def stop(signum, frame):
                self.stop = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            failures = 0
            try:
                while not self.stop:
                    try:
                        self.tick()
                        failures = 0
                    except (OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
                        failures += 1
                        self.publish("waiting_environment", f"{type(exc).__name__}: 环境或来源暂不可用，等待重试")
                        if failures >= 6:
                            self.notify("environment", "知识处理持续等待运行环境", "连续6次无法读取来源或运行环境；未消耗模型任务，服务继续等待。")
                    if once:
                        break
                    for _ in range(self.cfg["poll_seconds"]):
                        if self.stop:
                            break
                        time.sleep(1)
            finally:
                self.publish("stopped", "服务已停止，断点保留")
                self.queue.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "pause", "resume", "evaluate", "reassess-target"))
    parser.add_argument("--root", type=Path, default=STATE_ROOT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch", help="Completed batch to independently evaluate")
    parser.add_argument("--input", type=Path, help="Actual sample review JSON; never generated by this command")
    args = parser.parse_args()
    if args.command == "run":
        Worker(args.root).run(args.once)
    elif args.command == "status":
        print(json.dumps(read(args.root / "status.json"), ensure_ascii=False))
    elif args.command == "evaluate":
        if not args.batch or not args.input:
            parser.error("evaluate requires --batch and --input")
        queue = Queue(args.root)
        try:
            print(json.dumps(queue.record_evaluation(args.batch, read(args.input)), ensure_ascii=False))
        finally:
            queue.close()
    elif args.command == "reassess-target":
        if not args.batch:
            parser.error("reassess-target requires --batch")
        queue = Queue(args.root)
        try:
            print(json.dumps(queue.refresh_target_readiness(args.batch), ensure_ascii=False))
        finally:
            queue.close()
    else:
        queue = Queue(args.root)
        queue.set("paused", args.command == "pause")
        queue.set("pause_reason", "用户暂停" if args.command == "pause" else "")
        if args.command == "resume":
            queue.set("consecutive_failures", 0)
        queue.close()
        print(json.dumps({"paused": args.command == "pause"}))


if __name__ == "__main__":
    main()
