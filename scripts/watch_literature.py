"""Independent, bounded recovery for one authorized biomed collection.

Runs as a user systemd timer, including when the application worker is down.
Never deletes data, edits budgets, bypasses cooldowns, or restarts shared Docker.
"""

import argparse
import fcntl
import hashlib
import json
import os
import smtplib
import ssl
import subprocess
import time
from contextlib import suppress
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

WORKER_CONTAINER = "biomed-worker"
DOCKER_STARTUP_ALERT_SECONDS = 30 * 60
STOP_RECOVERY_WORKER = """\
import os, signal
from pathlib import Path
pid = int(Path('/app/var/.curation-worker-recovery.pid').read_text().strip())
proc = Path('/proc') / str(pid)
cmd = (proc / 'cmdline').read_bytes().split(b'\\0')
if cmd[:4] != [b'python', b'-u', b'manage.py', b'curation_worker']:
    raise SystemExit('Recovery process identity changed')
if (proc / 'task' / str(pid) / 'children').read_text().strip():
    raise SystemExit('Recovery worker is processing a job')
os.kill(pid, signal.SIGTERM)
"""


def stamp(value):
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0
    )


def command(cfg, args, timeout=45):
    result = subprocess.run(
        args,
        cwd=cfg["project"],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Process output can contain environment details; keep it out of email/logs.
        raise RuntimeError(f"{args[0]} exited {result.returncode}")
    return result.stdout


def recovery_worker_active(cfg):
    """Use the temporary worker only when it owns the shared singleton lock."""
    for directory in cfg.get("storage_paths", []):
        root = Path(directory)
        try:
            namespace_pid = int((root / ".curation-worker-recovery.pid").read_text().strip())
            lock = (root / ".curation-worker-heartbeat.lock").stat()
            key = f"{os.major(lock.st_dev):02x}:{os.minor(lock.st_dev):02x}:{lock.st_ino}"
            for line in Path("/proc/locks").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "FLOCK" and fields[3] == "WRITE" and fields[5] == key:
                    pid = int(fields[4])
                    if recovery_process_matches(pid, namespace_pid):
                        return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def recovery_process_matches(pid, namespace_pid):
    try:
        root = Path("/proc") / str(pid)
        command_line = (root / "cmdline").read_bytes().split(b"\0")
        status = (root / "status").read_text()
        namespace = next(line for line in status.splitlines() if line.startswith("NSpid:"))
        return (
            command_line[:4] == [b"python", b"-u", b"manage.py", b"curation_worker"]
            and int(namespace.split()[-1]) == namespace_pid
        )
    except (OSError, ValueError, StopIteration):
        return False


def recovery_root(cfg):
    for directory in cfg.get("storage_paths", []):
        root = Path(directory)
        if (root / ".curation-worker-recovery.pid").is_file():
            return root
    return None


def waiting_for_worker_lock(root, pid):
    lock = (root / ".curation-worker-heartbeat.lock").stat()
    key = f"{os.major(lock.st_dev):02x}:{os.minor(lock.st_dev):02x}:{lock.st_ino}"
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if (len(fields) >= 9 and fields[1:5] == ["->", "FLOCK", "ADVISORY", "WRITE"]
                and fields[5] == str(pid) and fields[6] == key):
            return True
    return False


def original_worker_matches(pid):
    try:
        args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return args[:3] == [b"python", b"manage.py", b"curation_worker"]
    except OSError:
        return False


def handoff_recovery_worker(cfg):
    """Stop the temporary worker only after its original replacement waits for the same lock."""
    if not recovery_worker_active(cfg):
        return "not_active"
    try:
        root = recovery_root(cfg)
        if root is None:
            return "waiting_for_original"
        template = '{{.State.Running}}|{{.State.Pid}}|{{range .Mounts}}{{if eq .Destination "/app/var"}}{{.Source}}{{end}}{{end}}'
        info = command(cfg, ["docker", "inspect", "--format", template, WORKER_CONTAINER], timeout=3).strip().split("|")
        if len(info) != 3 or info[0] != "true" or Path(info[2]).resolve() != root.resolve():
            return "waiting_for_original"
        pid = int(info[1])
        if not original_worker_matches(pid) or not waiting_for_worker_lock(root, pid):
            return "waiting_for_original"
        # Recheck the lock owner after querying Docker; the worker may have exited.
        if not recovery_worker_active(cfg):
            return "not_active"
        command(cfg, ["docker", "exec", "biomed-web", "python", "-c", STOP_RECOVERY_WORKER], timeout=15)
        return "signal_sent"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return "waiting_for_original"


def guard(cfg, action="status"):
    # Avoid resolving unrelated Compose services during a single-worker probe.
    container = "biomed-web" if recovery_worker_active(cfg) else WORKER_CONTAINER
    args = [
        "docker",
        "exec",
        container,
        "python",
        "manage.py",
        "literature_guard",
        cfg["collection_id"],
        "--action",
        action,
    ]
    if action != "status":
        args += ["--limits-hash", cfg["limits_hash"]]
    return json.loads(command(cfg, args))


def docker_startup(cfg):
    """Recognize only systemd-confirmed startup, without operating on Docker."""
    try:
        output = command(
            cfg,
            ["systemctl", "show", "docker.service",
             "--property=ActiveState,InvocationID,ExecMainStartTimestampMonotonic"],
            timeout=5,
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if values.get("ActiveState") != "activating":
            return None
        started = int(values["ExecMainStartTimestampMonotonic"]) / 1_000_000
        invocation = values["InvocationID"]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if started <= 0 or not invocation or not boot:
            return None
        return {"identity": boot + ":" + invocation,
                "elapsed_seconds": max(0, time.monotonic() - started)}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        # Unverified/other daemon states retain the existing bounded recovery.
        return None


def wait_for_docker_startup(cfg, state, now):
    startup = docker_startup(cfg)
    if not startup:
        if state.pop("docker_startup", None):
            # Time spent waiting for the daemon is not a stalled collection.
            state["last_progress"] = now
        return False
    state["docker_startup"] = startup
    state["attention"] = "Docker 正在启动恢复，采集保留断点等待服务就绪"
    if startup["elapsed_seconds"] >= DOCKER_STARTUP_ALERT_SECONDS:
        notify(
            cfg, state, "docker-startup:" + startup["identity"],
            "Docker 启动超过 30 分钟，采集等待恢复",
            "systemd 确认 Docker 仍在启动；采集保留断点和原限速，监控继续等待，未消耗资料 worker 恢复次数。",
            now,
        )
    return True


def storage_problem(cfg):
    try:
        return inspect_storage(cfg)
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ):
        # Unverifiable storage must never enter the process-restart path.
        return "持久存储无法核验：挂载、标记或文件系统不可读"


def inspect_storage(cfg):
    mount = json.loads(
        command(
            cfg, ["findmnt", "--json", "-T", "/appdata", "-o", "UUID,TARGET,OPTIONS"]
        )
    )["filesystems"][0]
    if (
        mount["uuid"] != cfg["storage_uuid"]
        or mount["target"] != "/appdata"
        or "rw" not in mount["options"].split(",")
    ):
        return "持久存储挂载缺失、只读或身份不符"
    for name in cfg["storage_paths"]:
        path = Path(name)
        if (path / ".biomed-storage").read_text().strip() != cfg["storage_marker"]:
            return "持久存储标记不符"
    for name in ["/data", "/appdata"]:
        stats = os.statvfs(name)
        if stats.f_bavail * stats.f_frsize < 2 * 1024**3:
            return name + " 剩余物理空间少于 2 GiB"
        if stats.f_files and stats.f_favail < 4096:
            return name + " 剩余 inode 少于 4096"
    return None


def notify(cfg, state, key, subject, body, now):
    notices = state.setdefault("notices", {})
    previous = notices.get(key, {})
    if previous.get("sent_at") or now < previous.get("next_attempt", 0):
        return
    server = None
    try:
        cfg_mail = json.loads(Path(cfg["mail_config"]).read_text())
        message = EmailMessage()
        message["From"] = cfg_mail["from_email"]
        message["To"] = ", ".join(cfg_mail["to"])
        message["Subject"] = "[biomed] " + subject
        digest = hashlib.sha256((cfg["collection_id"] + key).encode()).hexdigest()
        message["Message-ID"] = f"<biomed-watch-{digest}@claw.163.com>"
        message.set_content(
            body
            + "\n\n批次："
            + cfg["collection_id"]
            + "\n文献页：http://192.168.124.2:50011/evidence/sources/papers/\n"
        )
        context = ssl.create_default_context()
        if cfg_mail["security"] == "ssl":
            server = smtplib.SMTP_SSL(
                cfg_mail["host"], cfg_mail["port"], timeout=15, context=context
            )
        elif cfg_mail["security"] == "starttls":
            server = smtplib.SMTP(cfg_mail["host"], cfg_mail["port"], timeout=15)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        else:
            raise ValueError("TLS required")
        server.login(cfg_mail["username"], cfg_mail["password"])
        if server.send_message(message):
            raise RuntimeError("recipient refused")
        notices[key] = {"sent_at": now}
    except (
        OSError,
        smtplib.SMTPException,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
    ) as exc:
        attempts = previous.get("attempts", 0) + 1
        notices[key] = {
            "attempts": attempts,
            "next_attempt": now + min(3600, 60 * 2 ** min(attempts, 6)),
            "error": type(exc).__name__,
        }
    finally:
        if server:
            with suppress(OSError):
                server.close()


def recovery_allowed(state, action, now):
    events = state.setdefault("actions", [])
    return sum(e["kind"] == action and e["at"] > now - 86400 for e in events) < 3


def act(cfg, state, action, now):
    # Record before the external operation: even an uncertain outcome consumes its allowance.
    state.setdefault("actions", []).append({"kind": action, "at": now})
    save_state(cfg, state)
    if action == "resume":
        guard(cfg, "resume")
    else:
        command(
            cfg,
            ["docker", "restart", "-t", "30", WORKER_CONTAINER],
            timeout=90,
        )


def halt(cfg, state, reason):
    # A terminal recovery failure stays stopped until explicitly investigated.
    state["halted"] = reason
    save_state(cfg, state)
    try:
        guard(cfg, "pause")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        try:
            command(
                cfg, ["docker", "stop", "-t", "30", WORKER_CONTAINER], timeout=45
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            state["halt_error"] = type(exc).__name__


def evaluate(cfg, state, status, now):
    if status["limits_hash"] != cfg["limits_hash"]:
        return "采集配置已改变，自动恢复停止，避免覆盖用户配置"
    progress_token = status.get("progress_token")
    if (
        progress_token is not None
        and progress_token != state.get("last_progress_token")
    ) or (
        progress_token is None and status["saved"] != state.get("last_saved")
    ):
        state["last_progress"] = now
    if progress_token is not None:
        state["last_progress_token"] = progress_token
    state.setdefault("last_progress", now)
    state["last_saved"] = status["saved"]
    if status.get("monitoring_complete", bool(status["completed_at"])):
        # Metadata completion does not end monitoring a later fulltext expansion.
        # Each approved budget has its own completion notice, so the old metadata
        # notice cannot suppress notification of the new fulltext batch.
        notice_key = "completed:" + status["limits_hash"]
        completed_at = status.get("monitoring_completed_at") or status["completed_at"]
        if now - stamp(completed_at) > 300:
            saved = status.get("saved")
            saved_text = f"已保存 {saved} 条元数据。" if saved is not None else ""
            notify(
                cfg,
                state,
                notice_key,
                "本批文献采集已完成或达到批准上限",
                f"本批采集已保存断点并停止。{saved_text}全文按本次批准预算处理，增量补扫尚未执行。",
                now,
            )
        state["finished"] = bool(
            state.get("notices", {}).get(notice_key, {}).get("sent_at")
        )
        return None
    state.pop("finished", None)
    if status["state"] == "paused":
        state["last_progress"] = now
        if status["message"].startswith("用户暂停"):
            return None
        if status["recoverable_pause"]:
            if stamp(status["retry_not_before"]) > now:
                return None
            if recovery_allowed(state, "resume", now):
                act(cfg, state, "resume", now)
                state["last_progress"] = now
                return None
            reason = "可恢复故障已自动续跑 3 轮仍未解决，保留断点等待进一步处理"
            halt(cfg, state, reason)
            return reason
        return "采集遇到不能安全自动绕过的暂停：" + status["message"]
    if status["state"] in ("running", "estimating"):
        if stamp(status["retry_not_before"]) > now:
            # Deliberate cooldown is not stalled processing after it expires.
            state["last_progress"] = now
            return None
        if (
            now - stamp(status["heartbeat"]) > 180
            or now - state["last_progress"] > 1200
        ):
            if recovery_allowed(state, "restart", now):
                act(cfg, state, "restart", now)
                state["last_progress"] = now
                return None
            reason = "后台进程已自动恢复 3 次仍无进展，需要进一步排障"
            halt(cfg, state, reason)
            return reason
    return None


def save_state(cfg, state):
    target = Path(cfg["state_file"])
    state["actions"] = state.get("actions", [])[-100:]
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    target = Path(cfg["state_file"])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads(target.read_text()) if target.exists() else {}
        # Still make the inexpensive status probe after completion: an operator
        # can approve another fulltext budget on this same metadata snapshot.
        now = time.time()
        problem = None
        try:
            if state.get("halted") and not args.dry_run:
                # Startup can be observed while a previous halt is retained.
                # Readiness never clears that halt without operator review.
                if wait_for_docker_startup(cfg, state, now):
                    state["checked_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
                    save_state(cfg, state)
                    return
                problem = state["halted"]
                raise RuntimeError(problem)
            problem = storage_problem(cfg)
            if problem:
                if not args.dry_run:
                    command(
                        cfg,
                        ["docker", "stop", "-t", "30", WORKER_CONTAINER],
                        timeout=45,
                    )
                raise RuntimeError(problem)
            if args.dry_run:
                startup = docker_startup(cfg)
                if startup:
                    print(json.dumps({"storage_ok": True, "docker_startup": startup,
                                      "status": None}, ensure_ascii=False))
                    return
            elif wait_for_docker_startup(cfg, state, now):
                state["checked_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
                save_state(cfg, state)
                return
            status = guard(cfg)
            if args.dry_run:
                print(
                    json.dumps(
                        {"storage_ok": True, "status": status}, ensure_ascii=False
                    )
                )
                return
            state["probe_failures"] = 0
            state["status"] = status
            problem = evaluate(cfg, state, status, now)
        except Exception as exc:
            if args.dry_run:
                raise
            state["probe_failures"] = state.get("probe_failures", 0) + 1
            if not problem and state["probe_failures"] >= 3:
                if recovery_allowed(state, "restart", now):
                    try:
                        act(cfg, state, "restart", now)
                        state["probe_failures"] = 0
                    except (OSError, RuntimeError, subprocess.TimeoutExpired):
                        problem = "后台服务自动重启失败，需进一步排障"
                else:
                    problem = "后台服务或数据库连续不可用，自动恢复额度已耗尽"
                    halt(cfg, state, problem)
            state["last_error_type"] = type(exc).__name__
        if problem:
            state["attention"] = problem
            notify(
                cfg,
                state,
                hashlib.sha256(problem.encode()).hexdigest(),
                "采集自动恢复需要处理",
                problem + "\n已保留原数据、断点和限速。",
                now,
            )
        else:
            state.pop("attention", None)
            if recovery_worker_active(cfg):
                state["worker_handoff"] = handoff_recovery_worker(cfg)
            elif state.get("worker_handoff") == "signal_sent":
                # The status command has succeeded in the original worker container.
                state["worker_handoff"] = "complete"
        state["checked_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        save_state(cfg, state)


if __name__ == "__main__":
    main()
