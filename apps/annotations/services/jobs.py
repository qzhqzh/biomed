"""Durable background annotation jobs backed by the database."""

from __future__ import annotations

import logging
import math
import time
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.annotations.engines.base import AnnotationError
from apps.annotations.models import AnnotationJob
from apps.annotations.knowledge import store as knowledge_store
from apps.annotations.services import annotate as annotate_service

logger = logging.getLogger("biotools.jobs")

_SUBMIT_CACHE_PREFIX = "annotate_job_submit:"


class JobSubmitTooSoon(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"请等待 {retry_after} 秒后再提交新任务")


def submit_cooldown_seconds() -> int:
    return int(getattr(settings, "BIOTOOLS_JOB_COOLDOWN_SECONDS", 10))


def _client_ident(request) -> str:
    if getattr(request, "auth", None):
        return f"key:{str(request.auth)[:24]}"

    # X-Forwarded-For is attacker-controlled unless the deployment explicitly
    # opts in after placing Django behind a trusted reverse proxy.
    if getattr(settings, "BIOTOOLS_TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"

    return f"ip:{request.META.get('REMOTE_ADDR', 'unknown')}"


def _retry_after(value: Any, default_seconds: int) -> int:
    try:
        remaining = math.ceil(float(value) - time.time())
    except (TypeError, ValueError):
        remaining = default_seconds
    return max(1, remaining)


def check_submit_cooldown(request) -> None:
    """Apply a backend-portable, race-safe submission cooldown.

    Django's default LocMemCache does not expose ``ttl()``. Store the expiry
    timestamp as the value and use ``cache.add`` so all standard Django cache
    backends can enforce the cooldown atomically.
    """

    seconds = submit_cooldown_seconds()
    if seconds <= 0:
        return

    key = _SUBMIT_CACHE_PREFIX + _client_ident(request)
    existing = cache.get(key)
    if existing is not None:
        raise JobSubmitTooSoon(_retry_after(existing, seconds))

    expires_at = time.time() + seconds
    if not cache.add(key, expires_at, timeout=seconds):
        existing = cache.get(key)
        raise JobSubmitTooSoon(_retry_after(existing, seconds))


def create_and_enqueue(
    *,
    variants: list[str],
    assembly: str,
    engines: list[str],
) -> AnnotationJob:
    """Persist a queued job for the independent worker process.

    The legacy function name is retained for API compatibility. No daemon
    thread is started inside the web worker, so Gunicorn recycling cannot lose
    an in-flight task.
    """

    return AnnotationJob.objects.create(
        status=AnnotationJob.Status.QUEUED,
        assembly=assembly,
        engines=engines,
        variants=variants,
        variant_count=len(variants),
    )


def claim_next_job() -> str | None:
    """Atomically move the oldest queued job to RUNNING and return its id."""

    with transaction.atomic():
        job_id = (
            AnnotationJob.objects.filter(status=AnnotationJob.Status.QUEUED)
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if job_id is None:
            return None

        claimed = AnnotationJob.objects.filter(
            pk=job_id,
            status=AnnotationJob.Status.QUEUED,
        ).update(
            status=AnnotationJob.Status.RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            error="",
            result=None,
        )

    return str(job_id) if claimed else None


def recover_stale_jobs(stale_after_seconds: int | None = None) -> int:
    """Requeue jobs left RUNNING by a terminated worker process."""

    seconds = stale_after_seconds
    if seconds is None:
        seconds = int(getattr(settings, "BIOTOOLS_JOB_STALE_AFTER_SECONDS", 1800))
    seconds = max(60, int(seconds))
    cutoff = timezone.now() - timedelta(seconds=seconds)
    return AnnotationJob.objects.filter(
        status=AnnotationJob.Status.RUNNING,
        started_at__lt=cutoff,
    ).update(
        status=AnnotationJob.Status.QUEUED,
        started_at=None,
        finished_at=None,
        error="",
    )


def run_job_safe(job_id: str) -> None:
    """Execute one already-claimed job and persist a terminal state."""

    close_old_connections()
    try:
        _run_job(job_id)
    except Exception:
        logger.exception("job runner crashed job_id=%s", job_id)
        try:
            AnnotationJob.objects.filter(
                pk=job_id,
                status=AnnotationJob.Status.RUNNING,
            ).update(
                status=AnnotationJob.Status.FAILED,
                error="内部任务执行异常",
                finished_at=timezone.now(),
            )
        except Exception:
            logger.exception("failed to mark job failed job_id=%s", job_id)
    finally:
        close_old_connections()


@knowledge_store.pin_snapshot()
def _run_job(job_id: str) -> None:
    try:
        job = AnnotationJob.objects.get(
            pk=job_id,
            status=AnnotationJob.Status.RUNNING,
        )
    except AnnotationJob.DoesNotExist:
        return

    runs: list[dict[str, Any]] = []

    for engine in job.engines:
        try:
            payload = annotate_service.annotate(
                variants=list(job.variants),
                assembly=job.assembly,
                engine=engine,
            )
            runs.append(
                {
                    "engine": engine,
                    "status": "ok",
                    "results": payload.get("results", []),
                }
            )
        except AnnotationError as exc:
            runs.append(
                {
                    "engine": engine,
                    "status": "error",
                    "error": str(exc),
                    "results": [],
                }
            )
        except Exception as exc:
            logger.exception("engine failed job_id=%s engine=%s", job_id, engine)
            runs.append(
                {
                    "engine": engine,
                    "status": "error",
                    "error": str(exc)[:500],
                    "results": [],
                }
            )

    ok_count = sum(1 for run in runs if run.get("status") == "ok")
    result = {"assembly": job.assembly, "runs": runs,
              "knowledge_version": knowledge_store.data_version()}
    finished_at = timezone.now()

    if ok_count == 0:
        fatal = "；".join(
            f"{run.get('engine')}: {run.get('error')}"
            for run in runs
            if run.get("error")
        ) or "全部引擎失败"
        AnnotationJob.objects.filter(
            pk=job_id,
            status=AnnotationJob.Status.RUNNING,
        ).update(
            status=AnnotationJob.Status.FAILED,
            error=fatal[:2000],
            result=result,
            finished_at=finished_at,
        )
    else:
        AnnotationJob.objects.filter(
            pk=job_id,
            status=AnnotationJob.Status.RUNNING,
        ).update(
            status=AnnotationJob.Status.SUCCEEDED,
            error="",
            result=result,
            finished_at=finished_at,
        )


def job_to_dict(job: AnnotationJob, *, include_result: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(job.id),
        "status": job.status,
        "assembly": job.assembly,
        "engines": job.engines,
        "variant_count": job.variant_count,
        "variants": job.variants if include_result else None,
        "error": job.error or None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if include_result:
        data["result"] = job.result
    else:
        data["result"] = None
        data["variants_preview"] = (job.variants or [])[:3]
    return data


def list_jobs(*, limit: int = 50) -> list[AnnotationJob]:
    limit = max(1, min(limit, 200))
    return list(AnnotationJob.objects.all()[:limit])
