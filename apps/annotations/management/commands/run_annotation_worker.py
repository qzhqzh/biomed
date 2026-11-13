"""Run the durable database-backed annotation worker."""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from apps.annotations.services import jobs as jobs_service
from config.database import transient_database_error


class Command(BaseCommand):
    help = "Poll the annotation job table and execute queued jobs"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process available jobs and exit when the queue is empty",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=float(getattr(settings, "BIOTOOLS_WORKER_POLL_SECONDS", 1.0)),
            help="Seconds to wait when the queue is empty",
        )
        parser.add_argument(
            "--stale-after",
            type=int,
            default=int(getattr(settings, "BIOTOOLS_JOB_STALE_AFTER_SECONDS", 1800)),
            help="Requeue RUNNING jobs older than this many seconds",
        )
        parser.add_argument(
            "--recovery-interval",
            type=float,
            default=60.0,
            help="Seconds between stale-job recovery scans",
        )

    def handle(self, *args, **options):
        once = bool(options["once"])
        poll_interval = max(0.1, float(options["poll_interval"]))
        stale_after = max(60, int(options["stale_after"]))
        recovery_interval = max(1.0, float(options["recovery_interval"]))
        last_recovery = 0.0

        self.stdout.write(
            self.style.SUCCESS(
                "annotation worker started "
                f"poll={poll_interval}s stale_after={stale_after}s"
            )
        )

        while True:
            try:
                now = time.monotonic()
                if now - last_recovery >= recovery_interval:
                    recovered = jobs_service.recover_stale_jobs(stale_after)
                    if recovered:
                        self.stdout.write(
                            self.style.WARNING(f"requeued {recovered} stale job(s)")
                        )
                    last_recovery = now
                job_id = jobs_service.claim_next_job()
            except OperationalError as exc:
                if not transient_database_error(exc):
                    raise
                close_old_connections()
                self.stderr.write(
                    "Annotation polling delayed by temporary database failure"
                )
                time.sleep(max(2, poll_interval))
                continue
            if job_id:
                self.stdout.write(f"running job {job_id}")
                jobs_service.run_job_safe(job_id)
                continue

            if once:
                break
            time.sleep(poll_interval)
