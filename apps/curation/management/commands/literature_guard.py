"""Small operational status/control endpoint for the independent host watchdog."""

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from apps.curation.collection_recovery import alert_pause, current_job
from apps.curation.collections import control_collection
from apps.curation.models import LiteratureCollection, WorkerHeartbeat
from apps.curation.worker_heartbeat import worker_last_seen


def limits_hash(limits):
    return hashlib.sha256(json.dumps(limits, sort_keys=True).encode()).hexdigest()


def recoverable_pause(run):
    if run.message.startswith(("用户暂停", "独立监控暂停")):
        return False
    alert = run.alerts.filter(resolved_at__isnull=True).order_by("-created_at").first()
    return bool(
        alert
        and (
            alert.reason == "retry_exhausted"
            or (
                alert.reason == "collection_paused"
                and run.message.startswith(
                    (
                        "资料磁盘可用空间",
                        "资料或数据库磁盘剩余 inode",
                    )
                )
            )
        )
    )


class Command(BaseCommand):
    help = "Lightweight collection status; guarded recovery never changes budgets or manual pauses"

    def add_arguments(self, parser):
        parser.add_argument("collection_id")
        parser.add_argument(
            "--action", choices=("status", "resume", "pause"), default="status"
        )
        parser.add_argument("--limits-hash", default="")

    def handle(self, *args, **options):
        run = LiteratureCollection.objects.get(pk=options["collection_id"])
        if options["action"] != "status":
            with transaction.atomic():
                run = LiteratureCollection.objects.select_for_update().get(pk=run.pk)
                if limits_hash(run.limits) != options["limits_hash"]:
                    raise CommandError("采集限速或预算已改变，自动监控拒绝覆盖用户配置")
                if options["action"] == "resume":
                    if run.state != "paused" or not recoverable_pause(run):
                        raise CommandError(
                            "该暂停需要核对，或由用户手动发起，不自动恢复"
                        )
                    if run.retry_not_before and run.retry_not_before > timezone.now():
                        raise CommandError("来源冷却尚未结束")
                if options["action"] == "pause":
                    if run.state in ("running", "estimating"):
                        run.state = "paused"
                        run.message = (
                            "独立监控暂停：自动恢复仍未解决故障，已保留断点和原限速"
                        )
                        run.save()
                        alert_pause(run, "monitor_paused")
                else:
                    run = control_collection(run.pk, options["action"])
        job = current_job(run)
        seen = worker_last_seen(WorkerHeartbeat)
        acquisition = run.estimate.get("acquisition", {})
        baseline_completed = acquisition.get("baseline_completed_at")
        monitoring_complete = run.state == "complete" or (
            bool(baseline_completed)
            and run.state == "paused"
            and run.phase == "metadata"
            and run.message.startswith("本范围候选元数据已扫描完成")
        )
        self.stdout.write(
            json.dumps(
                {
                    "id": str(run.pk),
                    "state": run.state,
                    "phase": run.phase,
                    "step": run.step,
                    "updated_at": run.updated_at,
                    # The watchdog needs progress, not a repeated million-row COUNT.
                    # An optional frozen count is exact; absent values stay unknown.
                    "saved": acquisition.get("baseline_saved"),
                    "progress_token": [run.phase, run.step],
                    "monitoring_complete": monitoring_complete,
                    "monitoring_completed_at": run.updated_at if monitoring_complete else None,
                    "limits": run.limits,
                    "limits_hash": limits_hash(run.limits),
                    "retry_not_before": run.retry_not_before,
                    "consecutive_errors": run.consecutive_errors,
                    "message": run.message,
                    "recoverable_pause": recoverable_pause(run),
                    "completed_at": baseline_completed,
                    "heartbeat": seen,
                    "job": {
                        "state": job.state,
                        "available_at": job.available_at,
                        "lease_until": job.lease_until,
                    }
                    if job
                    else None,
                    "alerts": list(
                        run.alerts.filter(resolved_at__isnull=True).values(
                            "reason",
                            "email_state",
                            "email_sent_at",
                            "message",
                        )
                    ),
                },
                cls=DjangoJSONEncoder,
                ensure_ascii=False,
            )
        )
