import gzip
import json
import smtplib
import tempfile
from datetime import timedelta
from datetime import timezone as datetime_timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.curation import collection_notifications as notifications
from apps.curation import collection_recovery as recovery
from apps.curation import collections, workflows
from apps.curation.collection_network import (
    CollectionHTTP,
    CollectionRetry,
    LeaseLost,
    retry_seconds,
)
from apps.curation.models import CollectionAlert, MaterialVersion, WorkItem
from apps.curation.network import SourceBlocked
from apps.curation.test_collections import article, articles, search_result

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=123"
BACKEND_ERROR = (
    "Search Backend failed: An error occurred while processing request. "
    "Status: 500. Source: /api/search/?r= Details: Search is temporarily "
    "unavailable. Please try again later."
)


class CollectionRecoveryTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        context = override_settings(
            CURATION_STORAGE_DIR=self.directory,
            CURATION_MAIL_CONFIG=self.directory / "smtp.json",
            CURATION_MODEL_BACKEND="",
        )
        context.enable()
        self.addCleanup(context.disable)

    def create(self):
        return collections.create_collection(
            "2021-10-15", "2026-10-15", {"max_fulltexts": 0}
        )

    def execute(self, data=None, error=None):
        job = workflows.claim()
        self.assertIsNotNone(job)
        with patch.object(
            CollectionHTTP, "fetch", return_value=data, side_effect=error
        ):
            workflows.execute(job)
        job.refresh_from_db()
        self.assertEqual(job.state, "succeeded", job.message)
        return job

    def make_due(self, run):
        run.refresh_from_db()
        job = recovery.current_job(run)
        self.assertIsNotNone(job)
        # Move the test clock forward rather than remove the persisted cooldown.
        return patch(
            "django.utils.timezone.now",
            return_value=job.available_at + timedelta(seconds=1),
        )

    def ready_page(self):
        run = self.create()
        self.execute(search_result(1))
        collections.control_collection(run.pk, "resume")
        partition = run.partitions.get()
        partition.state, partition.pmids, partition.count = "captured", ["123"], 1
        partition.save()
        self.execute()
        run.refresh_from_db()
        return run

    def test_retry_then_success_preserves_page_and_resets_streak(self):
        run = self.ready_page()
        original = list(run.entries.values_list("pmid", "metadata_version_id"))
        self.execute(error=CollectionRetry("HTTP 400"))
        run.refresh_from_db()
        self.assertEqual(run.consecutive_errors, 1)
        self.assertEqual(
            list(run.entries.values_list("pmid", "metadata_version_id")), original
        )
        self.assertIsNone(workflows.claim())
        with self.make_due(run):
            self.execute(articles(article()))
        run.refresh_from_db()
        self.assertEqual(run.consecutive_errors, 0)
        self.assertIsNone(run.retry_not_before)
        self.assertEqual(run.message, "")
        self.assertEqual(run.entries.count(), 1)
        self.assertEqual(MaterialVersion.objects.count(), 1)
        self.assertFalse(run.alerts.exists())

    def test_embedded_search_backend_error_retries_without_advancing_partition(self):
        run = self.create()
        self.execute(search_result(1))
        collections.control_collection(run.pk, "resume")
        run.refresh_from_db()
        limits = dict(run.limits)
        before = list(run.partitions.values())
        # Real 2026-10-26 ESearch response: HTTP 200, nested uppercase ERROR,
        # no count. Its raw response remains recorded by CollectionHTTP.
        self.execute(json.dumps({"esearchresult": {"ERROR": BACKEND_ERROR}}).encode())
        run.refresh_from_db()
        self.assertEqual(run.state, "running")
        self.assertEqual(run.consecutive_errors, 1)
        self.assertEqual(run.limits, limits)
        self.assertEqual(list(run.partitions.values()), before)
        self.assertFalse(run.entries.exists())
        self.assertFalse(run.alerts.exists())
        self.assertIsNone(workflows.claim())
        with self.make_due(run):
            self.execute(search_result(1, ["123"]))
        run.refresh_from_db()
        self.assertEqual(run.consecutive_errors, 0)
        self.assertIsNone(run.retry_not_before)
        self.assertEqual(run.partitions.get().pmids, ["123"])
        self.assertEqual(run.partitions.get().state, "captured")
        self.assertEqual(run.limits, limits)

    def test_repeated_embedded_backend_errors_pause_once_with_checkpoint_intact(self):
        run = self.create()
        before = list(run.partitions.values())
        for attempt in range(recovery.MAX_FAILURES):
            if attempt:
                with self.make_due(run):
                    self.execute(json.dumps({"error": BACKEND_ERROR}).encode())
            else:
                self.execute(json.dumps({"error": BACKEND_ERROR}).encode())
            run.refresh_from_db()
            self.assertEqual(run.consecutive_errors, attempt + 1)
            self.assertEqual(list(run.partitions.values()), before)
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.alerts.count(), 1)
        self.assertEqual(run.alerts.get().reason, "retry_exhausted")
        self.assertFalse(run.entries.exists())

    def test_permanent_or_unknown_search_errors_still_pause_without_retry(self):
        responses = (
            {},
            {"esearchresult": {"ERROR": "Invalid query"}},
            {"esearchresult": {"ERROR": "Search Backend failed: Status: 400"}},
            {"esearchresult": {"ERROR": "Unknown source problem"}},
            {"esearchresult": {"ERROR": BACKEND_ERROR, "errorlist": {"FieldNotFound": ["bad"]}}},
            {"error": "Invalid query", "esearchresult": {"ERROR": BACKEND_ERROR}},
        )
        for response in responses:
            with self.subTest(response=response):
                run = self.create()
                self.execute(json.dumps(response).encode())
                run.refresh_from_db()
                self.assertEqual(run.state, "paused")
                self.assertEqual(run.consecutive_errors, 0)
                self.assertIsNone(run.retry_not_before)
                self.assertEqual(run.partitions.get().state, "pending")
                self.assertFalse(run.entries.exists())

    def test_pause_resume_preserves_long_retry_after(self):
        run = self.create()
        self.execute(error=CollectionRetry("HTTP 429", retry_after=7200))
        run.refresh_from_db()
        until = run.retry_not_before
        collections.control_collection(run.pk, "pause")
        recovery.reconcile_collection_jobs()
        self.assertFalse(run.alerts.exists())
        run = collections.control_collection(run.pk, "resume")
        self.assertEqual(run.retry_not_before, until)
        self.assertGreaterEqual(recovery.current_job(run).available_at, until)
        self.assertIsNone(workflows.claim())

    def test_pause_during_failed_request_keeps_manual_pause_and_cooldown(self):
        run = self.create()

        def pause_then_fail(*args):
            collections.control_collection(run.pk, "pause")
            raise CollectionRetry("HTTP 429", retry_after=600)

        self.execute(error=pause_then_fail)
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertIn("用户暂停", run.message)
        self.assertFalse(run.alerts.exists())
        self.assertGreater(
            run.retry_not_before, timezone.now() + timedelta(seconds=599)
        )
        recovery.reconcile_collection_jobs()
        self.assertEqual(WorkItem.objects.filter(state="queued").count(), 0)

    def test_orphan_jobs_recover_with_bounded_backoff_and_one_alert(self):
        run = self.create()
        for index in range(6):
            run.refresh_from_db()
            recovery.current_job(run).delete()
            now = timezone.now()
            recovery.reconcile_collection_jobs()
            run.refresh_from_db()
            self.assertEqual(run.consecutive_errors, index + 1)
            if index < 5:
                job = recovery.current_job(run)
                self.assertGreaterEqual(
                    (job.available_at - now).total_seconds(),
                    recovery.RETRY_DELAYS[index],
                )
                step = run.step
                recovery.reconcile_collection_jobs()
                run.refresh_from_db()
                self.assertEqual(run.step, step)
        self.assertEqual(run.state, "paused")
        recovery.reconcile_collection_jobs()
        self.assertEqual(run.alerts.count(), 1)
        self.assertEqual(run.alerts.get().reason, "retry_exhausted")
        data = self.client.get("/api/v1/curation/collection-alerts/").json()
        self.assertEqual(data["count"], 1)
        self.assertFalse(data["mail_configured"])
        collections.control_collection(run.pk, "resume")
        alert = run.alerts.get()
        self.assertIsNotNone(alert.resolved_at)
        self.assertEqual(alert.email_state, "cancelled")

    def test_failed_worker_requeues_and_old_lease_cannot_commit(self):
        run = self.ready_page()
        job = workflows.claim()
        workflows.finish(job, "failed", message="worker interrupted", retry=True)
        run.refresh_from_db()
        self.assertEqual(run.consecutive_errors, 1)
        self.assertEqual(run.state, "running")
        with self.assertRaises(LeaseLost):
            CollectionHTTP(job).fetch(EFETCH)
        self.assertFalse(workflows.finish(job, "succeeded"))
        with self.make_due(run):
            self.execute(articles(article()))
        self.assertEqual(MaterialVersion.objects.count(), 1)

    def test_transient_http_statuses_only_retry_expected_requests(self):
        self.create()
        job = workflows.claim()
        cases = (
            (EFETCH, 400, CollectionRetry),
            (EFETCH, 408, CollectionRetry),
            (EFETCH, 503, CollectionRetry),
            (EFETCH, 403, SourceBlocked),
            (EFETCH.replace("efetch", "esearch"), 400, SourceBlocked),
            (EFETCH.replace("db=pubmed", "db=pmc"), 400, SourceBlocked),
        )
        with patch("apps.curation.collection_network.time.sleep"):
            for url, code, error in cases:
                with (
                    self.subTest(url=url, code=code),
                    patch("apps.curation.collection_network.build_opener") as opener,
                ):
                    opener.return_value.open.side_effect = HTTPError(
                        url, code, "source error", {"Retry-After": "600"}, None
                    )
                    with self.assertRaises(error) as caught:
                        CollectionHTTP(job).fetch(url)
                    if error is SourceBlocked:
                        self.assertNotIsInstance(caught.exception, CollectionRetry)

    def test_truncated_response_is_retryable_and_reservation_retained(self):
        run = self.create()
        job = workflows.claim()
        response = MagicMock(status=200)
        response.headers = {}
        response.read.side_effect = IncompleteRead(b"partial", 100)
        with patch("apps.curation.collection_network.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaises(CollectionRetry):
                CollectionHTTP(job).fetch(EFETCH)
        receipt = run.fetches.get()
        self.assertEqual(receipt.state, "failed")
        self.assertEqual(receipt.charged_bytes, 8 * 1024**2)

    def test_truncated_gzip_is_retryable(self):
        self.create()
        job = workflows.claim()
        response = MagicMock(status=200)
        response.headers = {"Content-Encoding": "gzip"}
        response.read.side_effect = [gzip.compress(b"metadata")[:-5], b""]
        with (
            patch("apps.curation.collection_network.build_opener") as opener,
            patch("apps.curation.collection_network.time.sleep"),
        ):
            opener.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaises(CollectionRetry):
                CollectionHTTP(job).fetch(EFETCH)

    def test_local_storage_error_is_distinct_from_network_failure(self):
        run = self.create()
        job = workflows.claim()
        response = MagicMock(status=200)
        response.headers = {}
        response.read.side_effect = [b"metadata", b""]
        with (
            patch("apps.curation.collection_network.build_opener") as opener,
            patch("apps.curation.collection_network.time.sleep"),
            patch(
                "apps.curation.collection_network.store_blob",
                side_effect=PermissionError("secret path"),
            ),
            self.assertLogs(
                "apps.curation.collection_network", level="WARNING"
            ) as logs,
        ):
            opener.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaisesMessage(CollectionRetry, "原始资料暂时无法保存"):
                CollectionHTTP(job).fetch(EFETCH)
        self.assertNotIn("secret path", " ".join(logs.output))
        receipt = run.fetches.get()
        self.assertEqual(receipt.http_status, 200)
        self.assertEqual(receipt.state, "failed")
        self.assertIn("原始资料", receipt.message)

    def test_retry_after_date_and_huge_value(self):
        with patch(
            "django.utils.timezone.now",
            return_value=timezone.datetime(2026, 10, 16, tzinfo=datetime_timezone.utc),
        ):
            self.assertEqual(retry_seconds("Fri, 16 Oct 2026 01:00:00 GMT"), 3600)
        self.assertEqual(retry_seconds("invalid"), 30)
        with self.assertRaises(SourceBlocked):
            retry_seconds("9" * 100)


class CollectionNotificationTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "smtp.json"
        context = override_settings(CURATION_MAIL_CONFIG=self.path)
        context.enable()
        self.addCleanup(context.disable)
        self.cfg = {
            "host": "smtp.example.org",
            "port": 25,
            "security": "starttls",
            "username": "sender@example.org",
            "password": "test-only-dummy",
            "from_email": "sender@example.org",
            "to": ["owner@example.org"],
        }
        self.path.write_text(json.dumps(self.cfg))
        self.run = collections.create_collection("2021-10-15", "2026-10-15")
        collections.control_collection(self.run.pk, "pause")
        WorkItem.objects.update(state="succeeded")
        self.alert = CollectionAlert.objects.create(
            collection=self.run, step=0, reason="retry_exhausted", message="测试暂停"
        )

    def send_job(self, effect=None):
        notifications.schedule_notifications()
        job = workflows.claim()
        self.assertEqual(job.kind, "notify_collection")
        with patch.object(notifications, "send_message", side_effect=effect) as send:
            workflows.execute(job)
        self.alert.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(job.state, "succeeded", job.message)
        return send

    def test_mail_configuration_absent_or_invalid_waits_without_sending(self):
        for value in (
            None,
            [],
            {**self.cfg, "security": "plain"},
            {**self.cfg, "to": [["@"]]},
        ):
            if value is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_text(json.dumps(value))
            notifications.schedule_notifications()
            self.alert.refresh_from_db()
            self.assertEqual(self.alert.email_state, "waiting_configuration")
            self.assertFalse(WorkItem.objects.filter(kind="notify_collection").exists())
        self.path.write_text(json.dumps(self.cfg))
        self.send_job().assert_called_once()
        self.assertEqual(self.alert.email_state, "sent")

    def test_outbox_deduplicates_and_records_server_acceptance(self):
        notifications.schedule_notifications()
        notifications.schedule_notifications()
        self.assertEqual(WorkItem.objects.filter(kind="notify_collection").count(), 1)
        self.send_job().assert_called_once()
        self.assertEqual(self.alert.email_state, "sent")
        self.assertEqual(self.alert.email_attempts, 1)
        self.assertIsNotNone(self.alert.email_sent_at)
        self.assertIn(str(self.run.pk), self.alert.email_message_id)
        notifications.schedule_notifications()
        self.assertEqual(WorkItem.objects.filter(kind="notify_collection").count(), 1)
        data = self.client.get("/api/v1/curation/collection-alerts/").json()
        self.assertEqual(data["results"][0]["email_state"], "sent")
        self.assertNotIn("test-only-dummy", json.dumps(data))

    def test_pending_completion_notice_survives_resume_without_duplicate(self):
        self.alert.reason = recovery.COMPLETED_REASON
        self.alert.message = "元数据已扫描完成"
        self.alert.save()
        run = collections.control_collection(self.run.pk, "resume")
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.resolved_at)
        self.assertEqual(self.alert.email_state, "pending")
        self.assertEqual(recovery.record_completion(run).pk, self.alert.pk)
        # The collection was resumed; finish its queued step without a network request.
        collections.control_collection(self.run.pk, "pause")
        WorkItem.objects.filter(kind="collect_literature").update(state="succeeded")
        send = self.send_job()
        self.assertIn("元数据采集完成", send.call_args.args[1])
        self.assertEqual(self.alert.email_state, "sent")

    def test_completion_mail_retries_with_same_message_id(self):
        self.alert.reason = recovery.COMPLETED_REASON
        self.alert.save()
        first = self.send_job(OSError())
        self.assertEqual(self.alert.email_state, "pending")
        CollectionAlert.objects.filter(pk=self.alert.pk).update(
            email_available_at=timezone.now()
        )
        second = self.send_job()
        self.assertEqual(self.alert.email_state, "sent")
        self.assertEqual(first.call_args.args[-1], second.call_args.args[-1])
        self.assertIn("元数据采集完成", second.call_args.args[1])
        self.assertEqual(
            self.client.get("/api/v1/curation/collection-alerts/").json()["count"], 0
        )

    def test_mail_failures_are_bounded_delayed_and_do_not_expose_response(self):
        message_ids = []
        for index in range(5):
            send = self.send_job(
                smtplib.SMTPAuthenticationError(535, b"test-only-dummy")
            )
            message_ids.append(send.call_args.args[-1])
            self.assertEqual(self.alert.email_attempts, index + 1)
            self.assertNotIn("test-only-dummy", self.alert.email_error)
            if index < 4:
                self.assertEqual(self.alert.email_state, "pending")
                self.assertGreater(self.alert.email_available_at, timezone.now())
                notifications.schedule_notifications()
                self.assertIsNone(workflows.claim())
                CollectionAlert.objects.filter(pk=self.alert.pk).update(
                    email_available_at=timezone.now()
                )
        self.assertEqual(self.alert.email_state, "failed")
        self.assertEqual(len(set(message_ids)), 1)
        notifications.schedule_notifications()
        self.assertIsNone(workflows.claim())

    def test_resume_cancels_pending_mail_and_manual_retry_respects_cooldown(self):
        self.send_job(OSError())
        old = WorkItem.objects.get(kind="notify_collection")
        WorkItem.objects.filter(pk=old.pk).update(state="failed")
        workflows.retry_job(old)
        with patch.object(notifications, "send_message") as send:
            workflows.execute(workflows.claim())
        send.assert_not_called()
        collections.control_collection(self.run.pk, "resume")
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.email_state, "cancelled")
        self.assertIsNotNone(self.alert.resolved_at)

    def test_dead_mail_process_before_send_is_recovered_and_bounded(self):
        for index in range(5):
            notifications.schedule_notifications()
            job = workflows.claim()
            workflows.finish(job, "failed", message="child failed")
            notifications.schedule_notifications()
            self.alert.refresh_from_db()
            self.assertEqual(self.alert.email_attempts, index + 1)
            if index < 4:
                CollectionAlert.objects.filter(pk=self.alert.pk).update(
                    email_available_at=timezone.now()
                )
        self.assertEqual(self.alert.email_state, "failed")
        notifications.schedule_notifications()
        self.assertIsNone(workflows.claim())

    def test_starttls_precedes_login_and_close_failure_does_not_resend(self):
        with patch.object(notifications.smtplib, "SMTP") as smtp:
            server = smtp.return_value
            server.send_message.return_value = {}
            server.close.side_effect = OSError()
            notifications.send_message(self.cfg, "Test", "Body", "<test@example.org>")
            calls = [call[0] for call in server.mock_calls]
            self.assertLess(calls.index("starttls"), calls.index("login"))
            self.assertLess(calls.index("login"), calls.index("send_message"))
            self.assertTrue(server.starttls.call_args.kwargs["context"].check_hostname)
        with patch.object(notifications.smtplib, "SMTP") as smtp:
            smtp.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError()
            with self.assertRaises(smtplib.SMTPNotSupportedError):
                notifications.send_message(
                    self.cfg, "Test", "Body", "<test@example.org>"
                )
            smtp.return_value.login.assert_not_called()
