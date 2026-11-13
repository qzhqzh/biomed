"""Low-write liveness signal, with a database fallback for older deployments."""

import fcntl
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.db import OperationalError, connections
from django.utils import timezone

from config.database import transient_database_error


def heartbeat_path():
    configured = getattr(settings, "CURATION_HEARTBEAT_FILE", None)
    if configured is not None:
        return Path(configured) if configured else None
    database = connections["default"].settings_dict
    name = str(database.get("NAME") or "")
    if (
        "sqlite3" not in database.get("ENGINE", "")
        or not name
        or name == ":memory:"
        or name.startswith("file:")
    ):
        return None
    return Path(name).parent / ".curation-worker-heartbeat"


def touch_worker_heartbeat():
    """Update non-durable liveness metadata; preserve the durable fallback on failure."""
    path = heartbeat_path()
    if path is None:
        return False
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags, 0o644)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            os.utime(fd, None)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


@contextmanager
def single_worker_lock():
    """Prevent two worker processes from claiming the same deployment's work."""
    path = heartbeat_path()
    if path is None:
        yield
        return
    path = path.with_name(path.name + ".lock")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Worker lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def worker_last_seen(model):
    """Read a fresh shared signal or fall back to the existing database record."""
    path = heartbeat_path()
    file_seen = None
    if path is not None:
        try:
            info = path.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                file_seen = datetime.fromtimestamp(
                    info.st_mtime, tz=datetime_timezone.utc
                )
                age = (timezone.now() - file_seen).total_seconds()
                if -5 <= age <= 20:
                    return file_seen
                if age < -5:
                    file_seen = None
        except (OSError, OverflowError, ValueError):
            pass
    try:
        beat = model.objects.filter(name="curation").first()
    except OperationalError as exc:
        if file_seen is None or not transient_database_error(exc):
            raise
        return file_seen
    database_seen = beat.last_seen if beat else None
    if file_seen and database_seen:
        return max(file_seen, database_seen)
    return file_seen or database_seen
