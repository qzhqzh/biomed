"""Persistent email outbox for collection completion and unexpected stops."""

import json
import smtplib
import ssl
from contextlib import suppress
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.curation.collection_network import check_lease
from apps.curation.collection_recovery import COMPLETED_REASON
from apps.curation.models import CollectionAlert, WorkItem

MAIL_DELAYS = (60, 300, 900, 3600)
MAX_MAIL_ATTEMPTS = len(MAIL_DELAYS) + 1


class MailNotConfigured(Exception):
    pass


def mail_config():
    try:
        path = settings.CURATION_MAIL_CONFIG
        if path.stat().st_size > 65536:
            raise ValueError()
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict):
            raise TypeError()
        for key in ("host", "username", "password", "from_email"):
            if not isinstance(cfg.get(key), str) or not cfg[key].strip():
                raise ValueError()
        validate_email(cfg["from_email"])
        if not isinstance(cfg.get("to"), list) or not 1 <= len(cfg["to"]) <= 5:
            raise ValueError()
        for address in cfg["to"]:
            if not isinstance(address, str):
                raise TypeError()
            validate_email(address)
        if type(cfg.get("port")) is not int or not 1 <= cfg["port"] <= 65535:
            raise ValueError()
        if cfg.get("security") not in ("ssl", "starttls"):
            raise ValueError()
        return cfg
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        raise MailNotConfigured("邮件通道待配置；请检查本地私有 SMTP 配置") from exc


def send_message(cfg, subject, body, message_id):
    message = EmailMessage()
    message["From"] = cfg["from_email"]
    message["To"] = ", ".join(cfg["to"])
    message["Subject"] = subject
    message["Date"] = format_datetime(timezone.now())
    message["Message-ID"] = message_id
    message.set_content(body)
    context = ssl.create_default_context()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
    try:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        server.login(cfg["username"], cfg["password"])
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # Closing after DATA was accepted must not turn a successful send into a retry.
        with suppress(OSError):
            server.close()
    return message_id


def schedule_notifications():
    from apps.curation.workflows import enqueue

    try:
        mail_config()
        configured = True
    except MailNotConfigured:
        configured = False
    rows = CollectionAlert.objects.filter(
        resolved_at__isnull=True,
        email_state__in=("pending", "waiting_configuration", "sending"),
    ).filter(
        Q(email_available_at__isnull=True) | Q(email_available_at__lte=timezone.now())
    )
    for pk in rows.values_list("pk", flat=True)[:10]:
        with transaction.atomic():
            alert = CollectionAlert.objects.select_for_update().get(pk=pk)
            if (
                alert.resolved_at
                or alert.email_state in ("sent", "failed", "cancelled")
                or (
                    alert.email_available_at
                    and alert.email_available_at > timezone.now()
                )
            ):
                continue
            if not configured:
                if alert.email_state != "waiting_configuration":
                    alert.email_state = "waiting_configuration"
                    alert.email_error = "邮件通道待配置，站内告警已保留"
                    alert.save()
                continue
            jobs = WorkItem.objects.filter(
                kind="notify_collection", payload__alert_id=str(pk)
            )
            if jobs.filter(state__in=("queued", "running")).exists():
                continue
            last_job = jobs.order_by("-created_at").first()
            if (
                last_job
                and last_job.state == "failed"
                and last_job.payload["attempt"] > alert.email_attempts
            ):
                # A subprocess may die before entering the send function. Count it
                # too so a broken runtime cannot restart this outbox forever.
                alert.email_attempts = last_job.payload["attempt"]
                alert.email_state = (
                    "failed" if alert.email_attempts >= MAX_MAIL_ATTEMPTS else "pending"
                )
                alert.email_available_at = timezone.now() + timedelta(
                    seconds=MAIL_DELAYS[
                        min(alert.email_attempts - 1, len(MAIL_DELAYS) - 1)
                    ]
                )
                alert.email_error = "邮件执行进程中断；" + (
                    "已达到重试上限"
                    if alert.email_state == "failed"
                    else "稍后自动重试"
                )
                alert.save()
                continue
            enqueue(
                "notify_collection",
                {
                    "alert_id": str(pk),
                    "attempt": alert.email_attempts + 1,
                    "sequence": jobs.count() + 1,
                },
            )


def notification_content(alert):
    run = alert.collection
    saved = run.entries.filter(metadata_version__isnull=False).count()
    notified_at = timezone.localtime(alert.created_at, timezone.get_fixed_timezone(480))
    common = (
        f"批次：{run.pk}\n范围：{run.start_date} 至 {run.end_date}\n"
        f"已入库：{saved:,} 条候选元数据\n"
        f"通知生成时间：{notified_at:%Y-%m-%d %H:%M:%S}（北京时间）\n"
    )
    if alert.reason == COMPLETED_REASON:
        fulltexts = run.entries.filter(fulltext_state__in=("parsed", "reused")).count()
        subject = "[biomed] 本次范围文献元数据采集完成"
        body = (
            "biomed 本次日期范围的候选元数据已扫描完成\n\n"
            + common
            + f"当前可用全文：{fulltexts:,} 篇（全文按独立预算采集）\n"
            + "原始资料、分类和完成时间已保留；后续增量更新另行安排。\n"
        )
    else:
        subject = "[biomed] 文献采集已暂停，需要处理"
        body = (
            "biomed 文献采集需要处理\n\n"
            + common
            + f"原因：{alert.message}\n失败次数：{run.consecutive_errors}\n"
            + "断点和原有限速已保留；请核对原因后续跑。\n"
        )
    return subject, body + "查看：http://192.168.124.2:50011/evidence/sources/papers/\n"


def deliver_notification(item):
    check_lease(item)
    with transaction.atomic():
        alert = (
            CollectionAlert.objects.select_for_update()
            .select_related("collection")
            .get(pk=item.payload["alert_id"])
        )
        if alert.email_state == "sent":
            return {"state": "sent", "message_id": alert.email_message_id}
        if alert.resolved_at:
            alert.email_state = "cancelled"
            alert.save(update_fields=["email_state"])
            return {"state": "cancelled"}
        if alert.email_available_at and alert.email_available_at > timezone.now():
            return {"state": "waiting"}
        if alert.email_attempts >= MAX_MAIL_ATTEMPTS:
            alert.email_state = "failed"
            alert.save(update_fields=["email_state"])
            return {"state": "failed"}
        try:
            cfg = mail_config()
        except MailNotConfigured:
            alert.email_state = "waiting_configuration"
            alert.email_error = "邮件通道待配置，站内告警已保留"
            alert.save(update_fields=["email_state", "email_error"])
            return {"state": "waiting_configuration"}
        alert.email_attempts += 1
        alert.email_state = "sending"
        alert.email_available_at = timezone.now() + timedelta(minutes=15)
        alert.delivery_token = item.lease_token
        alert.email_message_id = (
            alert.email_message_id
            or f"<biomed-collection-{alert.pk}-{alert.collection_id}@biomed.local>"
        )
        alert.save()
    subject, body = notification_content(alert)
    try:
        send_message(cfg, subject, body, alert.email_message_id)
    except Exception as exc:  # noqa: BLE001 — retain only the error type, never SMTP credentials/content
        check_lease(item)
        failed = alert.email_attempts >= MAX_MAIL_ATTEMPTS
        CollectionAlert.objects.filter(
            pk=alert.pk, delivery_token=item.lease_token
        ).update(
            email_state="failed" if failed else "pending",
            email_available_at=timezone.now()
            + timedelta(
                seconds=MAIL_DELAYS[min(alert.email_attempts - 1, len(MAIL_DELAYS) - 1)]
            ),
            email_error=f"邮件发送失败（{type(exc).__name__}）；"
            + ("已达到重试上限" if failed else "稍后自动重试"),
            delivery_token=None,
        )
        return {"state": "failed" if failed else "retry"}
    check_lease(item)
    CollectionAlert.objects.filter(pk=alert.pk, delivery_token=item.lease_token).update(
        email_state="sent",
        email_sent_at=timezone.now(),
        email_error="",
        delivery_token=None,
    )
    return {"state": "sent", "message_id": alert.email_message_id}
