"""Bounded retries and queue repair. User-paused collections are never restarted."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.curation.models import CollectionAlert, LiteratureCollection, WorkItem

ACTIVE = ("estimating", "running")
RETRY_DELAYS = (60, 300, 900, 1800, 3600)
MAX_FAILURES = len(RETRY_DELAYS) + 1
COMPLETED_REASON = "collection_completed"


def retry_delay(previous_failures, retry_after=0):
    return max(retry_after, RETRY_DELAYS[min(previous_failures, len(RETRY_DELAYS) - 1)])


def current_job(run):
    from apps.curation.workflows import job_key

    return WorkItem.objects.filter(
        dedup_key=job_key(
            "collect_literature", {"collection_id": str(run.pk), "step": run.step}
        )
    ).first()


def alert_pause(run, reason="collection_paused"):
    CollectionAlert.objects.get_or_create(
        collection=run,
        step=run.step,
        defaults={"reason": reason, "message": run.message},
    )


def record_completion(run):
    """Caller holds the collection lock; one completion notice survives resumes."""
    return CollectionAlert.objects.get_or_create(
        collection=run,
        reason=COMPLETED_REASON,
        defaults={"step": run.step, "message": run.message},
    )[0]


def record_retry(run, message, delay):
    """Caller holds the collection lock and persists/schedules the resulting state."""
    run.retry_not_before = max(
        run.retry_not_before or timezone.now(),
        timezone.now() + timedelta(seconds=delay),
    )
    if run.state not in ACTIVE:
        return
    run.consecutive_errors += 1
    if run.consecutive_errors >= MAX_FAILURES:
        run.state = "paused"
        run.message = (
            f"{message}；连续失败 {run.consecutive_errors} 次，已暂停并保留断点，"
            "请检查来源或运行环境后续跑"
        )
    else:
        run.message = (
            f"{message}；连续失败 {run.consecutive_errors}/{MAX_FAILURES} 次，"
            f"至少等待 {int(delay)} 秒后自动重试；断点和限速保持不变"
        )


def recover_collection_job(item, message):
    """Called only after the old attempt has been fenced or made terminal."""
    from apps.curation.collections import schedule

    with transaction.atomic():
        run = (
            LiteratureCollection.objects.select_for_update()
            .filter(pk=item.payload.get("collection_id"))
            .first()
        )
        if not run or run.state not in ACTIVE or run.step != item.payload.get("step"):
            return
        delay = retry_delay(run.consecutive_errors)
        record_retry(run, message, delay)
        if run.state == "paused":
            alert_pause(run, "retry_exhausted")
        run.step += 1
        run.save()
        if run.state in ACTIVE:
            schedule(run, delay)


def reconcile_collection_jobs():
    """Repair a missing/terminal current step, never an active lease or cooldown."""
    from apps.curation.collections import schedule

    for pk in LiteratureCollection.objects.filter(state__in=ACTIVE).values_list(
        "pk", flat=True
    ):
        with transaction.atomic():
            run = LiteratureCollection.objects.select_for_update().get(pk=pk)
            if run.state not in ACTIVE:
                continue
            job = current_job(run)
            if job and job.state in ("queued", "running"):
                continue
            delay = retry_delay(run.consecutive_errors)
            record_retry(run, "采集队列步骤中断，正在从已保存断点恢复", delay)
            if run.state == "paused":
                alert_pause(run, "retry_exhausted")
            run.step += 1
            run.save()
            if run.state in ACTIVE:
                schedule(run, delay)


def resolve_alerts(run):
    alerts = run.alerts.exclude(reason=COMPLETED_REASON)
    alerts.filter(resolved_at__isnull=True).update(resolved_at=timezone.now())
    alerts.filter(
        email_state__in=("pending", "waiting_configuration", "failed")
    ).update(email_state="cancelled")
