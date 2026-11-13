"""Polling reuses counters while state and retry controls remain live."""

from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.curation.classification import taxonomy_snapshot
from apps.curation.collections import DEFAULT_LIMITS, collection_data, collection_entry_counts
from apps.curation.models import (
    CollectionEntry, CollectionFetch, LiteratureCollection, Material, MaterialVersion,
)


class CollectionStatusCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.run = LiteratureCollection.objects.create(
            start_date="2021-10-15", end_date="2026-10-15",
            taxonomy=taxonomy_snapshot(), query="test", limits=DEFAULT_LIMITS,
            state="paused",
        )
        CollectionEntry.objects.create(collection=self.run, pmid="1")
        CollectionFetch.objects.create(
            collection=self.run, url="https://example.test/1", charged_bytes=100,
        )

    def read(self, *, cached=True):
        return collection_data(self.run, summary=True, cached=cached)

    def grow(self):
        CollectionEntry.objects.create(
            collection=self.run, pmid="2", fulltext_state="unavailable",
        )
        CollectionFetch.objects.create(
            collection=self.run, url="https://example.test/2", state="failed",
            charged_bytes=40, wire_bytes=40, decoded_bytes=40,
        )

    def test_repeated_poll_reads_neither_entries_nor_fetches(self):
        before = self.read()
        self.grow()
        with CaptureQueriesContext(connection) as queries:
            after = self.read()
        self.assertEqual(after["discovered"], 1)
        self.assertEqual(after["metrics"]["requests"], 1)
        self.assertEqual(after["counts_as_of"], before["counts_as_of"])
        self.assertEqual(after["metrics_as_of"], before["metrics_as_of"])
        for query in queries:
            self.assertNotIn("curation_collectionentry", query["sql"])
            self.assertNotIn("curation_collectionfetch", query["sql"])

    def test_cached_counts_do_not_hide_live_state_or_retry(self):
        self.read()
        self.run.state, self.run.phase, self.run.step = "running", "fulltext", 9
        self.run.message = "等待来源允许窗口"
        self.run.retry_not_before = timezone.now() + timedelta(hours=1)
        self.run.limits = self.run.limits | {"max_fulltexts": 2000}
        self.run.save()
        result = self.read()
        self.assertEqual((result["state"], result["phase"], result["step"]),
                         ("running", "fulltext", 9))
        self.assertEqual(result["message"], self.run.message)
        self.assertEqual(result["recovery"]["retry_not_before"], self.run.retry_not_before)
        self.assertEqual(result["limits"]["max_fulltexts"], 2000)

    def test_uncached_read_recomputes_exact_counts_and_reserved_budget(self):
        self.read()
        self.grow()
        result = self.read(cached=False)
        self.assertEqual(result["discovered"], 2)
        self.assertEqual(result["fulltexts"], {"pending": 1, "unavailable": 1})
        self.assertEqual(result["metrics"]["requests"], 2)
        self.assertEqual(result["metrics"]["charged_bytes"], 140)
        self.assertEqual(result["metrics"]["errors"], 1)
        self.assertEqual(result["metrics"]["reservations"], 1)

    def test_counter_and_ledger_snapshots_expire_independently(self):
        self.read()
        self.grow()
        cache.delete(f"curation:collection-entry-counts:v1:{self.run.pk}")
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(result["discovered"], 2)
        self.assertEqual(result["metrics"]["requests"], 1)
        self.assertTrue(any("curation_collectionentry" in q["sql"] for q in queries))
        self.assertFalse(any("curation_collectionfetch" in q["sql"] for q in queries))
        cache.delete(f"curation:collection-fetch-metrics:v1:{self.run.pk}")
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(result["metrics"]["requests"], 2)
        self.assertFalse(any("curation_collectionentry" in q["sql"] for q in queries))
        self.assertTrue(any("curation_collectionfetch" in q["sql"] for q in queries))

    def test_counts_stream_index_without_temporary_grouping(self):
        CollectionEntry.objects.create(collection=self.run, pmid="2", reused=True)
        for pmid, reused in (("3", False), ("4", True)):
            material = Material.objects.create(source_key=f"pubmed:{pmid}", title=pmid)
            version = MaterialVersion.objects.create(
                material=material, sha256=pmid.zfill(64),
                media_type="application/pubmed+xml", filename="source.xml", rights="test",
            )
            CollectionEntry.objects.create(
                collection=self.run, pmid=pmid, reused=reused,
                fulltext_state="parsed", metadata_version=version,
            )
        with CaptureQueriesContext(connection) as queries:
            result = collection_entry_counts(self.run)
        self.assertEqual((result["discovered"], result["saved"], result["reused"]),
                         (4, 2, 2))
        self.assertEqual(result["fulltexts"], {"pending": 2, "parsed": 2})
        self.assertEqual(len(queries), 1)
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + queries[0]["sql"])
            plan = " ".join(str(row) for row in cursor.fetchall()).upper()
        self.assertIn("COVERING INDEX COLLECT_ENTRY_FULLTEXT", plan)
        self.assertNotIn("TEMP B-TREE", plan)
