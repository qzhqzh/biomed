"""Capture RxClass in the background, then import only a validated full snapshot.

The caller supplies the production environment, DATABASE_PATH and
CURATION_STORAGE_DIR explicitly. No credentials are read or written here.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from apps.curation import rxclass_source as source
from apps.curation.network import TransientSourceError

CAPTURE_RETRY_DELAYS = (60, 300, 900)
IMPORT_RETRY_DELAYS = (15, 60, 300)


class ImportFailure(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_progress(directory):
    path = directory / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("counts", {})
    except (OSError, ValueError, TypeError, AttributeError):
        # Status reporting must not turn a corrupt snapshot into a success;
        # fetch_snapshot independently rejects it in the capture phase.
        return {}


def pause(status, save, phase, error):
    status.update(state="paused", phase=phase, error=error, paused_at=utc_now())
    status.pop("next_retry_at", None)
    save()


def wait_retry(status, save, delay, sleeper):
    status["next_retry_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat()
    save()
    remaining = delay
    while remaining:
        interval = min(60, remaining)
        sleeper(interval)
        remaining -= interval
        save()
    status.pop("next_retry_at", None)


def validate_import_result(result, manifest):
    if (
        not isinstance(result, dict)
        or not result.get("id")
        or result.get("namespace") != "RXCLASS_EPC"
        or result.get("version") != manifest["version"]
        or result.get("active") is not True
        or not isinstance(result.get("counts"), dict)
    ):
        raise ImportFailure("Importer did not confirm an active RxClass dataset")
    for key in ("classes_total", "classes_completed", "memberships"):
        if result["counts"].get(key) != manifest["counts"][key]:
            raise ImportFailure("Importer/source counts differ")


def sync_snapshot(
    output_dir,
    database,
    library,
    *,
    fetcher=source.fetch_snapshot,
    runner=subprocess.run,
    sleeper=time.sleep,
):
    directory = Path(output_dir).resolve()
    database, library = Path(database).resolve(), Path(library).resolve()
    if (
        not directory.is_relative_to(source.RESOURCE_ROOT.resolve())
        or directory == source.RESOURCE_ROOT.resolve()
    ):
        raise source.SnapshotError("Sync output must be inside the resource directory")
    if not database.is_file() or not library.is_dir():
        raise source.SnapshotError(
            "Explicit existing database and library are required"
        )
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".sync.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise source.SnapshotError("Another RxClass sync is active") from exc
        return _sync(directory, database, library, fetcher, runner, sleeper)


def _sync(directory, database, library, fetcher, runner, sleeper):
    status_path = directory / "sync-status.json"
    binding = {"database": str(database), "library": str(library)}
    previous = json.loads(status_path.read_text()) if status_path.exists() else {}
    if previous and previous.get("target") != binding:
        raise source.SnapshotError("Existing sync is bound to a different target")
    status = {
        **previous,
        "schema_version": "rxclass-background-sync-v1",
        "target": binding,
        "source_manifest": "manifest.json",
        "started_at": previous.get("started_at", utc_now()),
        "events": previous.get("events", []),
        "state": "running",
        "phase": "capture",
        "pid": os.getpid(),
    }
    status.pop("error", None)
    status.pop("next_retry_at", None)

    def save():
        status["updated_at"] = utc_now()
        status["counts"] = read_progress(directory)
        source.atomic_json(status_path, status)

    save()
    retry_index = 0
    last_failed_progress = -1
    while True:
        status.update(state="running", phase="capture")
        save()
        try:
            manifest = fetcher(directory)
            if manifest.get("state") != "completed":
                raise source.SnapshotError("Capture returned an incomplete snapshot")
            break
        except TransientSourceError as exc:
            progress = read_progress(directory).get("classes_completed", 0)
            if progress != last_failed_progress:
                retry_index = 0
            last_failed_progress = progress
            if retry_index == len(CAPTURE_RETRY_DELAYS):
                pause(
                    status,
                    save,
                    "capture",
                    "Temporary source failures exceeded this checkpoint's retry budget",
                )
                return status
            delay = CAPTURE_RETRY_DELAYS[retry_index]
            retry_index += 1
            status["events"].append(
                {
                    "at": utc_now(),
                    "phase": "capture",
                    "error": str(exc),
                    "progress": progress,
                    "retry_after_seconds": delay,
                }
            )
            status["state"] = "waiting_retry"
            wait_retry(status, save, delay, sleeper)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # Format, integrity, source-version or filesystem failures are not
            # treated as transport failures and never trigger an automatic import.
            pause(status, save, "capture", f"{type(exc).__name__}: {exc}")
            return status

    manifest_sha = hashlib.sha256(
        (directory / "manifest.json").read_bytes()
    ).hexdigest()
    if previous.get("state") == "completed":
        if previous.get("manifest_sha256") != manifest_sha:
            pause(
                status, save, "validation", "Completed sync's source manifest changed"
            )
            return status
        try:
            validate_import_result(previous.get("import_result"), manifest)
        except ImportFailure as exc:
            pause(status, save, "validation", str(exc))
            return status
        status.update(state="completed", phase="completed")
        save()
        return status
    status.update(state="running", phase="import", manifest_sha256=manifest_sha)
    save()
    environment = {
        **os.environ,
        "DATABASE_PATH": str(database),
        "CURATION_STORAGE_DIR": str(library),
    }
    command = [
        sys.executable,
        str(PROJECT_ROOT / "manage.py"),
        "import_reference_resources",
        str(directory),
    ]
    for attempt in range(len(IMPORT_RETRY_DELAYS) + 1):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            result = runner(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            pause(
                status,
                save,
                "import",
                f"{type(exc).__name__}: importer could not complete",
            )
            return status
        stdout_file = f"import-{stamp}.stdout.log"
        stderr_file = f"import-{stamp}.stderr.log"
        source.immutable_bytes(directory / stdout_file, result.stdout.encode())
        source.immutable_bytes(directory / stderr_file, result.stderr.encode())
        status["events"].append(
            {
                "at": utc_now(),
                "phase": "import",
                "returncode": result.returncode,
                "stdout_file": stdout_file,
                "stderr_file": stderr_file,
            }
        )
        if result.returncode:
            locked = any(
                term in result.stderr.lower()
                for term in ("database is locked", "database table is locked")
            )
            if locked and attempt < len(IMPORT_RETRY_DELAYS):
                status["state"] = "waiting_retry"
                wait_retry(status, save, IMPORT_RETRY_DELAYS[attempt], sleeper)
                status["state"] = "running"
                save()
                continue
            pause(
                status,
                save,
                "import",
                f"Importer exited {result.returncode}; see {stderr_file}",
            )
            return status
        try:
            receipt = json.loads(result.stdout)
            validate_import_result(receipt, manifest)
        except (ValueError, ImportFailure) as exc:
            pause(status, save, "import", f"{type(exc).__name__}: {exc}")
            return status
        status.update(
            state="completed",
            phase="completed",
            completed_at=utc_now(),
            import_result=receipt,
        )
        save()
        return status
    raise AssertionError("Unreachable import retry state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--library", required=True)
    args = parser.parse_args()
    try:
        status = sync_snapshot(args.output, args.database, args.library)
    except (OSError, source.SnapshotError) as exc:
        print(json.dumps({"state": "paused", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "state": status["state"],
                "phase": status["phase"],
                "counts": status.get("counts", {}),
                "error": status.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if status["state"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
