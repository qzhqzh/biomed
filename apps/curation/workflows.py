"""Database queue with idempotency, fenced claims and append-only attempt history."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.curation.models import WorkAttempt, WorkItem
from apps.curation.network import SourceBlocked, TransientSourceError

LEASE_SECONDS = 90


def enqueue(kind, payload):
    encoded = json.dumps(
        {"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    key = hashlib.sha256(encoded.encode()).hexdigest()
    return WorkItem.objects.get_or_create(
        dedup_key=key, defaults={"kind": kind, "payload": payload}
    )[0]


def recover_expired():
    now = timezone.now()
    for item in WorkItem.objects.filter(state="running", lease_until__lt=now):
        with transaction.atomic():
            changed = WorkItem.objects.filter(
                pk=item.pk,
                state="running",
                lease_token=item.lease_token,
                lease_until__lt=now,
            ).update(
                state="queued" if item.attempts < 3 else "failed",
                lease_token=None,
                lease_until=None,
                available_at=now,
                message="执行进程中断，已恢复排队。"
                if item.attempts < 3
                else "执行进程多次中断，请检查运行环境后重试。",
                updated_at=now,
            )
            if changed:
                WorkAttempt.objects.filter(
                    token=item.lease_token, outcome="running"
                ).update(outcome="interrupted", finished_at=now, message="任务租约过期")


def claim():
    recover_expired()
    for item in WorkItem.objects.filter(
        state="queued", available_at__lte=timezone.now()
    ).order_by("created_at")[:10]:
        token = uuid.uuid4()
        with transaction.atomic():
            changed = WorkItem.objects.filter(pk=item.pk, state="queued").update(
                state="running",
                attempts=F("attempts") + 1,
                lease_token=token,
                lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS),
                updated_at=timezone.now(),
                message="",
            )
            if changed:
                item.refresh_from_db()
                WorkAttempt.objects.create(job=item, number=item.attempts, token=token)
                return item
    return None


def finish(item, state, result=None, message="", retry=False):
    now = timezone.now()
    state = "queued" if retry and item.attempts < 3 else state
    with transaction.atomic():
        changed = WorkItem.objects.filter(
            pk=item.pk, state="running", lease_token=item.lease_token
        ).update(
            state=state,
            **({"result": result} if result is not None else {}),
            message=message[:2000],
            lease_token=None,
            lease_until=None,
            available_at=now + timedelta(seconds=30 * 2 ** min(item.attempts - 1, 5)),
            updated_at=now,
        )
        if changed:
            WorkAttempt.objects.filter(token=item.lease_token).update(
                outcome="retry" if state == "queued" else state,
                finished_at=now,
                message=message[:2000],
            )
    return bool(changed)


def dispatch(item):
    from apps.curation.materials import (
        acquire_pmc,
        acquire_pubmed,
        parse_material,
        pubmed_search,
    )
    from apps.curation.review import extract_candidates, review_ai
    from apps.curation.standards import sync_standard

    handlers = {
        "sync_standard": lambda: sync_standard(item.payload["namespace"]),
        "pubmed_search": lambda: pubmed_search(item.payload["query"]),
        "acquire_pubmed": lambda: acquire_pubmed(item.payload["pmid"]),
        "acquire_pmc": lambda: acquire_pmc(
            item.payload["material_id"], item.payload["pmcid"]
        ),
        "parse": lambda: parse_material(
            item.payload["version_id"], item.payload["parser_version"]
        ),
        "extract": lambda: extract_candidates(item),
        "review_ai": lambda: review_ai(item),
    }
    if item.kind not in handlers:
        raise SourceBlocked("未知的工作流类型")
    return handlers[item.kind]()


def execute(item):
    from apps.curation.review import ModelNotConfigured

    try:
        return finish(item, "succeeded", result=dispatch(item))
    except ModelNotConfigured as exc:
        return finish(item, "waiting_configuration", message=str(exc))
    except SourceBlocked as exc:
        return finish(item, "blocked", message=str(exc))
    except TransientSourceError as exc:
        return finish(item, "failed", message=str(exc), retry=True)
    except Exception:  # noqa: BLE001 — worker boundary persists a safe failure for every task
        # Avoid copying uploaded content, model credentials or external response bodies into logs.
        import logging

        logging.getLogger(__name__).error(
            "curation_job_failed job=%s kind=%s", item.pk, item.kind
        )
        return finish(
            item,
            "failed",
            message="任务处理失败，原始资料已保留。请检查解析格式或适配器配置后重试。",
        )


def retry_job(item):
    return WorkItem.objects.filter(
        pk=item.pk, state__in=["failed", "blocked", "waiting_configuration"]
    ).update(
        state="queued",
        available_at=timezone.now(),
        message="",
        updated_at=timezone.now(),
    )
