"""One supervised worker, bounded subprocesses, heartbeat and crash recovery."""

import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError
from django.utils import timezone

from apps.curation.models import WorkerHeartbeat, WorkItem
from apps.curation.workflows import LEASE_SECONDS, claim, finish

_last_heartbeat = 0.0


def heartbeat():
    global _last_heartbeat
    if time.monotonic() - _last_heartbeat < 10:
        return False
    # A single UPDATE avoids a deferred read-to-write transaction upgrade on SQLite.
    if not WorkerHeartbeat.objects.filter(name="curation").update(
        last_seen=timezone.now()
    ):
        WorkerHeartbeat.objects.get_or_create(name="curation")
    _last_heartbeat = time.monotonic()
    return True


class Command(BaseCommand):
    help = "Process persisted material/standard/evidence workflows. Run one instance per deployment."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stopped = False
        process = None

        def stop(signum, frame):
            nonlocal stopped
            stopped = True
            if process is not None:
                process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopped:
            try:
                heartbeat()
                item = claim()
            except OperationalError as exc:
                # Idle polling must survive the same temporary contention as active jobs.
                # Keep the old heartbeat timestamp: an unavailable database is not healthy.
                if not any(
                    message in str(exc).lower()
                    for message in ("database is locked", "database table is locked")
                ):
                    raise
                self.stderr.write("Worker polling delayed by database contention")
                time.sleep(2)
                continue
            if not item:
                if options["once"]:
                    return
                time.sleep(2)
                continue
            started = time.monotonic()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(settings.BASE_DIR / "manage.py"),
                    "curation_execute",
                    str(item.pk),
                    str(item.lease_token),
                ],
                cwd=settings.BASE_DIR,
                env=os.environ.copy(),
            )
            while process.poll() is None and not stopped:
                try:
                    renewed = heartbeat()
                    if renewed:
                        WorkItem.objects.filter(
                            pk=item.pk, state="running", lease_token=item.lease_token
                        ).update(
                            lease_until=timezone.now()
                            + timedelta(seconds=LEASE_SECONDS)
                        )
                except OperationalError:
                    # A transient heartbeat failure must not terminate an otherwise healthy child.
                    self.stderr.write("Worker heartbeat delayed by database contention")
                if time.monotonic() - started > 600:
                    process.kill()
                    process.wait()
                    finish(
                        item,
                        "failed",
                        message="任务超过 10 分钟执行上限，原始资料已保留。",
                    )
                    break
                time.sleep(2)
            if stopped and process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.returncode:
                finish(
                    item,
                    "failed",
                    message="执行进程中断；任务将按重试规则恢复。",
                    retry=True,
                )
            process = None
            if options["once"]:
                return
