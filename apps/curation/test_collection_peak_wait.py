import io
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfoNotFoundError

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.curation import collections, workflows
from apps.curation.collection_network import (
    CollectionHTTP,
    CollectionRetry,
    CollectionStopped,
    pmc_off_peak_delay,
)
from apps.curation.models import CollectionFetch, LiteratureCollection, WorkItem

PMC_URL = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/?verb=GetRecord"


class PMCHoursTests(SimpleTestCase):
    def test_eastern_weekday_boundaries_and_daylight_saving(self):
        for stamp, delay in (
            ("2026-01-05T09:59:59+00:00", 0),  # Monday 04:59:59 EST
            ("2026-01-05T10:00:00+00:00", 16 * 3600),
            ("2026-01-06T01:59:59+00:00", 1),
            ("2026-01-06T02:00:00+00:00", 0),
            ("2026-07-06T08:59:59+00:00", 0),  # Monday 04:59:59 EDT
            ("2026-07-06T09:00:00+00:00", 16 * 3600),
            ("2026-07-07T00:59:59+00:00", 1),
            ("2026-07-07T01:00:00+00:00", 0),
            ("2026-03-09T09:00:00+00:00", 16 * 3600),  # After DST starts
            ("2026-11-02T10:00:00+00:00", 16 * 3600),  # After DST ends
            ("2026-11-07T18:00:00+00:00", 0),  # Saturday
            ("2026-11-08T18:00:00+00:00", 0),  # Sunday
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(pmc_off_peak_delay(datetime.fromisoformat(stamp)), delay)

    def test_missing_timezone_database_uses_next_safe_utc_window(self):
        with patch(
            "apps.curation.collection_network.ZoneInfo",
            side_effect=ZoneInfoNotFoundError,
        ):
            for stamp, delay in (
                ("2026-11-06T00:00:00+00:00", 2 * 3600),
                ("2026-11-06T01:59:59+00:00", 1),
                ("2026-11-06T02:00:00+00:00", 0),
                ("2026-11-06T08:59:59+00:00", 0),
                ("2026-11-06T09:00:00+00:00", 17 * 3600),
                ("2026-11-06T23:00:00+00:00", 3 * 3600),
                ("2026-11-07T18:00:00+00:00", 8 * 3600),  # Conservative weekend
            ):
                with self.subTest(stamp=stamp):
                    self.assertEqual(
                        pmc_off_peak_delay(datetime.fromisoformat(stamp)), delay
                    )


class PMCBulkWaitTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(
            override_settings(
                CURATION_STORAGE_DIR=Path(directory.name), CURATION_MODEL_BACKEND=""
            )
        )
        self.now = self.enterContext(
            patch(
                "apps.curation.collection_network.timezone.now",
                return_value=datetime(2026, 11, 6, 18, tzinfo=timezone.utc),
            )
        )
        self.enterContext(patch("apps.curation.collection_network.time.sleep"))
        self.enterContext(
            patch(
                "apps.curation.collection_network.shutil.disk_usage",
                return_value=MagicMock(free=2 * 1024**3),
            )
        )
        self.enterContext(
            patch(
                "apps.curation.collection_network.os.statvfs",
                return_value=MagicMock(f_files=100000, f_favail=20000),
            )
        )
        self.opener = self.enterContext(
            patch("apps.curation.collection_network.build_opener")
        )
        response = MagicMock()
        response.read.side_effect = io.BytesIO(b"<result/>").read
        response.headers = {}
        response.status = 200
        response.__enter__.return_value = response
        self.opener.return_value.open.return_value = response

    def create(self, max_fulltexts=2000):
        run = collections.create_collection("2021-11-06", "2026-11-06")
        run.limits["max_fulltexts"] = max_fulltexts
        run.save(update_fields=["limits"])
        WorkItem.objects.filter(state="queued").update(available_at=self.now.return_value)
        return run, workflows.claim()

    def test_bulk_waits_before_first_request_without_reserving_budget(self):
        run, item = self.create()
        with self.assertRaises(CollectionRetry) as caught:
            CollectionHTTP(item).fetch(PMC_URL)
        self.assertEqual(caught.exception.retry_after, 8 * 3600)
        self.assertTrue(caught.exception.scheduling_wait)
        self.assertFalse(run.fetches.exists())
        self.opener.assert_not_called()

    def test_recent_global_requests_still_protect_small_batches(self):
        run, item = self.create(max_fulltexts=50)
        previous = LiteratureCollection.objects.create(
            start_date=run.start_date,
            end_date=run.end_date,
            query=run.query,
            taxonomy=run.taxonomy,
            limits=run.limits,
            state="completed",
        )
        CollectionFetch.objects.bulk_create(
            [
                CollectionFetch(collection=previous, url=PMC_URL, charged_bytes=0)
                for _ in range(100)
            ]
        )
        with self.assertRaises(CollectionRetry) as caught:
            CollectionHTTP(item).fetch(PMC_URL)
        self.assertEqual(caught.exception.retry_after, 8 * 3600)
        self.assertFalse(run.fetches.exists())
        self.assertEqual(previous.fetches.count(), 100)
        self.opener.assert_not_called()
        previous.fetches.update(
            started_at=self.now.return_value - timedelta(hours=24, seconds=1)
        )
        self.assertEqual(CollectionHTTP(item).fetch(PMC_URL), b"<result/>")
        self.assertEqual(run.fetches.count(), 1)

    def test_small_batch_below_recent_threshold_can_run_during_peak(self):
        run, item = self.create(max_fulltexts=100)
        self.assertEqual(CollectionHTTP(item).fetch(PMC_URL), b"<result/>")
        self.assertEqual(run.fetches.get().charged_bytes, len(b"<result/>"))
        self.opener.return_value.open.assert_called_once()

    def test_bulk_can_run_at_first_off_peak_second(self):
        self.now.return_value = datetime(2026, 11, 7, 2, tzinfo=timezone.utc)
        run, item = self.create()
        self.assertEqual(CollectionHTTP(item).fetch(PMC_URL), b"<result/>")
        self.assertEqual(run.fetches.count(), 1)
        self.opener.return_value.open.assert_called_once()

    def test_bulk_limit_does_not_delay_pubmed_metadata(self):
        run, item = self.create()
        self.assertEqual(
            CollectionHTTP(item).fetch("https://eutils.ncbi.nlm.nih.gov/a"),
            b"<result/>",
        )
        self.assertEqual(run.fetches.count(), 1)

    def test_compact_budget_queries_preserve_reservations_and_limits(self):
        run, item = self.create()
        run.limits["max_requests"] = 2
        run.save(update_fields=["limits"])
        url = "https://eutils.ncbi.nlm.nih.gov/a"
        # An interrupted request remains reserved and counts toward both limits.
        CollectionFetch.objects.create(collection=run, url=url, charged_bytes=7)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(CollectionHTTP(item).fetch(url), b"<result/>")
        ledger_queries = [
            entry["sql"]
            for entry in queries.captured_queries
            if entry["sql"].startswith("SELECT")
            and 'FROM "curation_collectionfetch"' in entry["sql"]
        ]
        budget_queries = [
            query for query in ledger_queries if "SUM(" in query or "COUNT(" in query
        ]
        self.assertEqual(len(budget_queries), 1)
        self.assertIn("SUM(", budget_queries[0])
        self.assertIn("COUNT(", budget_queries[0])
        last_query = next(query for query in ledger_queries if "ORDER BY" in query)
        self.assertEqual(
            last_query.split(" FROM ")[0].split(" AS ")[0],
            'SELECT "curation_collectionfetch"."started_at"',
        )
        self.assertRegex(
            last_query,
            r'ORDER BY (?:"curation_collectionfetch"\."started_at"|1) DESC LIMIT 1',
        )
        self.assertEqual(run.fetches.count(), 2)
        with self.assertRaisesRegex(CollectionStopped, "请求次数上限"):
            CollectionHTTP(item).fetch(url)
        run.limits.update(max_requests=3, max_bytes=7 + len(b"<result/>"))
        run.save(update_fields=["limits"])
        with self.assertRaisesRegex(CollectionStopped, "累计响应字节上限"):
            CollectionHTTP(item).fetch(url)
        self.assertEqual(run.fetches.count(), 2)
        self.assertEqual(sum(run.fetches.values_list("charged_bytes", flat=True)), 16)
        self.opener.return_value.open.assert_called_once()

    def test_wait_is_persisted_and_queue_resumes_without_manual_action(self):
        run, item = self.create()
        with patch(
            "apps.curation.collections.search",
            side_effect=lambda http, *_args: http.fetch(PMC_URL),
        ):
            workflows.execute(item)
        run.refresh_from_db()
        item.refresh_from_db()
        expected = self.now.return_value + timedelta(hours=8)
        self.assertEqual(item.state, "succeeded", item.message)
        self.assertEqual(run.state, "estimating")
        self.assertEqual(run.retry_not_before, expected)
        self.assertFalse(run.fetches.exists())
        self.assertEqual(WorkItem.objects.get(state="queued").available_at, expected)
        self.assertIsNone(workflows.claim())
        self.now.return_value = expected
        resumed = workflows.claim()
        self.assertIsNotNone(resumed)
        with patch("apps.curation.collections.search", return_value={"count": "0"}):
            workflows.execute(resumed)
        run.refresh_from_db()
        self.assertEqual(run.state, "ready")
        self.assertEqual(run.consecutive_errors, 0)
        self.assertIsNone(run.retry_not_before)
