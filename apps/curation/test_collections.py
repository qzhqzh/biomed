import gzip
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfoNotFoundError

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.curation import collections, workflows
from apps.curation.classification import classify, taxonomy_snapshot
from apps.curation.collection_network import (
    CollectionHTTP,
    CollectionRetry,
    CollectionStopped,
    pmc_peak_hours,
)
from apps.curation.models import (
    CollectionFetch,
    LiteratureClassification,
    Material,
    MaterialVersion,
    Passage,
    WorkItem,
)
from apps.curation.network import SourceBlocked
from apps.curation.storage import store_blob


def search_result(count, ids=(), *, translation=None):
    result = {"count": str(count), "idlist": list(ids)}
    if translation is not None:
        result["querytranslation"] = translation
    return json.dumps(
        {"esearchresult": result}
    ).encode()


def article(pmid="123", pmc="", title="Lung carcinoma randomized trial"):
    return f"""<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
    <ArticleTitle>{title}</ArticleTitle><Abstract><AbstractText>Patients with breast cancer and lymphoma.</AbstractText></Abstract>
    <PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
    </Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>
    {f'<ArticleId IdType="pmc">{pmc}</ArticleId>' if pmc else ""}
    </ArticleIdList></PubmedData></PubmedArticle>"""


def articles(*items):
    return ("<PubmedArticleSet>" + "".join(items) + "</PubmedArticleSet>").encode()


def book_article(pmid="456"):
    return f"""<PubmedBookArticle><BookDocument><PMID>{pmid}</PMID>
    <ArticleIdList><ArticleId IdType="bookaccession">NBK123</ArticleId></ArticleIdList>
    <Book><BookTitle>Reference book</BookTitle><PubDate><Year>1993</Year></PubDate></Book>
    <ArticleTitle>Hereditary gastric cancer</ArticleTitle><PubDate><Year>2026</Year></PubDate>
    <Language>eng</Language><Abstract><AbstractText>Inherited cancer risk.</AbstractText></Abstract>
    <ReferenceList><Reference><ArticleIdList><ArticleId IdType="pmc">PMC999</ArticleId></ArticleIdList></Reference></ReferenceList>
    </BookDocument></PubmedBookArticle>"""


class CollectionTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = override_settings(
            CURATION_STORAGE_DIR=Path(directory.name), CURATION_MODEL_BACKEND=""
        )
        context.enable()
        self.addCleanup(context.disable)

    def create(self, **limits):
        return collections.create_collection("2021-10-15", "2026-10-15", limits)

    def tick(self, response=None, effect=None):
        item = workflows.claim()
        self.assertIsNotNone(item)
        with patch.object(
            CollectionHTTP, "fetch", return_value=response, side_effect=effect
        ) as fetch:
            workflows.execute(item)
        item.refresh_from_db()
        self.assertEqual(item.state, "succeeded", item.message)
        return fetch

    def ready(self, **limits):
        run = self.create(**limits)
        self.tick(search_result(100))
        collections.control_collection(run.pk, "resume")
        return run

    def seed_ids(self, run, ids):
        partition = run.partitions.get()
        partition.state = "captured"
        partition.pmids = ids
        partition.count = len(ids)
        partition.save()

    def test_book_chapter_and_journal_batch_preserves_identity_and_kind(self):
        from apps.curation.management.commands.literature_classify import (
            reclassify_collection,
        )

        run = self.ready(max_items=2, max_fulltexts=0)
        self.seed_ids(run, ["123", "456"])
        self.tick()
        self.tick(articles(article(), book_article()))
        self.tick()
        self.tick()
        chapter = Material.objects.get(source_key="pubmed:456")
        self.assertEqual(chapter.kind, Material.Kind.OTHER)
        self.assertEqual(chapter.metadata["record_type"], "book_chapter")
        self.assertEqual(chapter.metadata["publication_date"], "2026")
        self.assertEqual(chapter.metadata["book_publication_date"], "1993")
        self.assertEqual(chapter.metadata["publication_types"], ["Book Chapter"])
        self.assertEqual(
            chapter.identifiers, {"pubmed": "456", "bookaccession": "NBK123"}
        )
        self.assertEqual(run.entries.filter(metadata_version__isnull=False).count(), 2)
        self.assertEqual(
            reclassify_collection(run.pk, taxonomy_snapshot())["reused"], 2
        )

    def test_mismatched_book_pmid_still_pauses_entire_page(self):
        run = self.ready(max_items=2, max_fulltexts=0)
        self.seed_ids(run, ["123", "456"])
        self.tick()
        self.tick(articles(article(), book_article("999")))
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertFalse(run.entries.filter(metadata_version__isnull=False).exists())

    def test_book_import_reuses_cached_version_without_network(self):
        from apps.curation.materials import acquire_pubmed

        with patch(
            "apps.curation.materials.fetch", return_value=articles(book_article())
        ):
            acquire_pubmed("456")
        run = self.ready(max_items=1, max_fulltexts=0)
        self.seed_ids(run, ["456"])
        self.tick()
        self.tick().assert_not_called()
        self.assertTrue(run.entries.get().reused)
        self.assertEqual(MaterialVersion.objects.count(), 1)

    def test_estimation_stops_before_acquisition_and_controls_are_validated(self):
        run = self.create()
        self.tick(search_result(1135208))
        run.refresh_from_db()
        self.assertEqual(run.state, "ready")
        self.assertEqual(run.estimate["candidate_count"], 1135208)
        self.assertFalse(Material.objects.exists())
        self.assertIsNone(workflows.claim())
        self.assertEqual(collections.collection_data(run)["saved"], 0)
        for invalid in (
            {"max_items": True},
            {"interval_seconds": 0},
            {"max_bytes": -1},
            {"other": 1},
        ):
            with self.assertRaises(SourceBlocked):
                collections.control_collection(run.pk, "resume", invalid)

    def test_date_splitting_does_not_truncate_at_ten_thousand(self):
        run = self.ready()
        self.tick(search_result(10001, [str(x) for x in range(1, 10000)]))
        self.assertEqual(run.partitions.filter(state="split").count(), 1)
        children = list(run.partitions.filter(state="pending").order_by("start_date"))
        self.assertEqual(len(children), 2)
        self.assertEqual(
            children[0].end_date + timedelta(days=1), children[1].start_date
        )
        self.assertEqual(children[0].start_date, run.start_date)
        self.assertEqual(children[1].end_date, run.end_date)
        self.assertTrue(all((child.uid_start, child.uid_end) == (1, 9999999999) for child in children))
        self.assertFalse(run.entries.exists())

    def test_date_splitting_preserves_an_existing_uid_range(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.uid_start, partition.uid_end = 100, 50000
        partition.save()
        self.tick(search_result(10001, translation="cancer AND 100:50000[UID]"))
        children = list(run.partitions.filter(state="pending"))
        self.assertEqual(len(children), 2)
        self.assertTrue(all((child.uid_start, child.uid_end) == (100, 50000) for child in children))

    def test_large_target_retains_page_and_network_bounds(self):
        run = self.ready(max_items=2000000, max_bytes=64 * 1024**3, page_size=1)
        self.seed_ids(run, ["123", "124"])
        self.tick()
        self.assertEqual(list(run.entries.values_list("pmid", flat=True)), ["123"])
        self.assertEqual(run.limits["interval_seconds"], 2)
        self.assertEqual(run.limits["bytes_per_second"], 256 * 1024)
        collections.control_collection(run.pk, "pause")
        for invalid in ({"max_items": 2000001}, {"max_bytes": 64 * 1024**3 + 1}):
            with self.assertRaises(SourceBlocked):
                collections.control_collection(run.pk, "resume", invalid)

    def full_observation(self, run, count=1135415):
        return {
            "query": run.query,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "candidate_total": count,
            "observed_at": timezone.now().isoformat(),
        }

    def test_full_plan_preserves_snapshot_anchor_and_rate_on_resume(self):
        run = self.ready(max_fulltexts=0)
        run.refresh_from_db()
        initial_estimate = dict(run.estimate)
        collections.control_collection(run.pk, "pause")
        resumed = collections.start_full_collection(run.pk, self.full_observation(run))
        plan = resumed.estimate["acquisition"]
        self.assertEqual(plan["reference_count"], 1135415)
        self.assertEqual(plan["baseline_started_at"], initial_estimate["observed_at"])
        self.assertEqual(
            resumed.estimate["candidate_count"], initial_estimate["candidate_count"]
        )
        self.assertIsNone(plan["baseline_completed_at"])
        self.assertIsNone(plan["incremental_watermark"])
        self.assertTrue(plan["recorded_at_local"].endswith("+08:00"))
        self.assertEqual(resumed.limits["interval_seconds"], 2)
        self.assertEqual(resumed.limits["bytes_per_second"], 256 * 1024)
        self.assertEqual(resumed.limits["max_fulltexts"], 0)
        collections.control_collection(run.pk, "pause")
        again = collections.start_full_collection(run.pk, self.full_observation(run))
        self.assertEqual(again.estimate["acquisition"], plan)

    def test_full_plan_rejects_wrong_scope_and_rolls_back_invalid_budget(self):
        run = self.ready()
        run.refresh_from_db()
        collections.control_collection(run.pk, "pause")
        wrong = self.full_observation(run) | {"query": "different query"}
        with self.assertRaises(SourceBlocked):
            collections.start_full_collection(run.pk, wrong)
        with self.assertRaises(SourceBlocked):
            collections.start_full_collection(
                run.pk, self.full_observation(run), {"max_bytes": -1}
            )
        run.refresh_from_db()
        self.assertNotIn("acquisition", run.estimate)
        self.assertEqual(run.state, "paused")

    def test_full_metadata_completion_is_separate_from_incremental_watermark(self):
        from apps.curation import collection_notifications as notifications

        run = self.ready(max_fulltexts=0)
        collections.control_collection(run.pk, "pause")
        collections.start_full_collection(run.pk, self.full_observation(run))
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertIsNotNone(run.estimate["acquisition"]["baseline_completed_at"])
        self.assertIsNone(run.estimate["acquisition"]["incremental_watermark"])
        self.assertEqual(run.state, "paused")
        self.assertIn("元数据已扫描完成", run.message)
        self.assertEqual(run.entries.get().fulltext_state, "pending")
        alert = run.alerts.get()
        self.assertEqual(alert.reason, "collection_completed")
        self.assertEqual(collections.collection_data(run)["recovery"]["open_alerts"], 0)
        self.assertEqual(
            self.client.get("/api/v1/curation/collection-alerts/").json()["count"], 0
        )
        with (
            patch.object(notifications, "mail_config", return_value={}),
            patch.object(notifications, "send_message") as send,
        ):
            notifications.schedule_notifications()
            workflows.execute(workflows.claim())
        self.assertIn("元数据采集完成", send.call_args.args[1])
        self.assertIn("已入库：1 条候选元数据", send.call_args.args[2])
        self.assertIn("当前可用全文：0 篇", send.call_args.args[2])
        self.assertNotIn("需要处理", send.call_args.args[1])
        alert.refresh_from_db()
        self.assertEqual(alert.email_state, "sent")
        # Another visit to the same completed snapshot must not send completion again.
        collections.control_collection(run.pk, "resume")
        self.tick()
        self.tick()
        self.assertEqual(run.alerts.count(), 1)
        alert.refresh_from_db()
        self.assertEqual(alert.email_state, "sent")

    def test_full_guard_stop_does_not_claim_scope_completion(self):
        run = self.ready(max_fulltexts=0)
        collections.control_collection(run.pk, "pause")
        collections.start_full_collection(
            run.pk, self.full_observation(run), {"max_items": 1}
        )
        self.seed_ids(run, ["123", "124"])
        self.tick()
        self.tick(articles(article()))
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertIsNone(run.estimate["acquisition"]["baseline_completed_at"])
        self.assertIsNone(run.estimate["acquisition"]["incremental_watermark"])
        self.assertEqual(run.state, "paused")
        self.assertIn("全量采集尚未完成", run.message)
        self.assertEqual(run.alerts.get().reason, "collection_paused")

    def test_complete_snapshot_without_remaining_fulltext_creates_notice(self):
        run = self.ready(max_fulltexts=0)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article()))
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, "complete")
        self.assertEqual(run.alerts.get().reason, "collection_completed")

    def test_error_after_metadata_scan_still_generates_pause_alert(self):
        run = self.ready(max_fulltexts=1)
        collections.control_collection(run.pk, "pause")
        collections.start_full_collection(run.pk, self.full_observation(run))
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        self.tick(effect=CollectionStopped("达到响应预算"))
        run.refresh_from_db()
        self.assertIsNotNone(run.estimate["acquisition"]["baseline_completed_at"])
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.alerts.get().reason, "collection_paused")

    def test_resumed_old_taxonomy_also_appends_current_rules(self):
        run = self.ready(max_items=1, max_fulltexts=0)
        run.taxonomy = taxonomy_snapshot("solid-tumor-rules-v1")
        run.save(update_fields=["taxonomy"])
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article()))
        self.assertEqual(
            set(
                LiteratureClassification.objects.values_list(
                    "taxonomy_version", flat=True
                )
            ),
            {"solid-tumor-rules-v1", "solid-tumor-rules-v3"},
        )
        run.refresh_from_db()
        self.assertEqual(run.taxonomy["version"], "solid-tumor-rules-v1")
        self.assertEqual(
            collections.collection_data(run)["classification_taxonomy_version"],
            "solid-tumor-rules-v3",
        )

    def test_batched_identity_checks_preserve_existing_and_same_page_conflicts(self):
        Material.objects.create(
            source_key="pubmed:999",
            title="Existing source",
            identifiers={"pubmed": "999", "doi": "10.TEST/existing"},
        )
        run = self.ready(max_items=3, max_fulltexts=0)
        self.seed_ids(run, ["123", "124", "125"])
        self.tick()

        def with_doi(pmid, doi):
            return article(pmid).replace(
                "</ArticleIdList>",
                f'<ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList>',
            )

        self.tick(
            articles(
                with_doi("123", "10.test/EXISTING"),
                with_doi("124", "10.test/new"),
                with_doi("125", "10.TEST/NEW"),
            )
        )
        self.assertEqual(Material.objects.count(), 4)
        self.assertEqual(
            dict(run.entries.values_list("pmid", "fulltext_state")),
            {"123": "identity_conflict", "124": "pending", "125": "identity_conflict"},
        )
        self.assertEqual(run.entries.filter(metadata_version__isnull=False).count(), 3)

    def test_single_day_overflow_splits_exhaustive_disjoint_uid_ranges(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.start_date = partition.end_date
        partition.save()
        self.tick(search_result(10001))
        run.refresh_from_db()
        partition.refresh_from_db()
        self.assertEqual(run.state, "running")
        self.assertEqual((partition.state, partition.count), ("split", 10001))
        children = list(run.partitions.filter(state="pending").order_by("uid_start"))
        self.assertEqual(len(children), 2)
        self.assertEqual((children[0].uid_start, children[-1].uid_end), (1, 9999999999))
        self.assertEqual(children[0].uid_end, 50000000)
        self.assertEqual(children[0].uid_end + 1, children[1].uid_start)
        self.assertTrue(all(child.start_date == child.end_date == partition.start_date for child in children))
        self.assertFalse(run.entries.exists())
        serialized = collections.collection_data(run, detail=True)["partitions"]
        self.assertTrue(all("uid_start" in child and "uid_end" in child for child in serialized))

    def test_single_uid_overflow_pauses_instead_of_splitting_forever(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.start_date = partition.end_date
        partition.uid_start = partition.uid_end = 123
        partition.save()
        self.tick(search_result(10001, translation="cancer AND 123[UID]"))
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertIn("单 UID", run.message)
        self.assertEqual(run.partitions.get().state, "pending")

    def test_open_uid_tail_splits_without_losing_higher_ids(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.start_date = partition.end_date
        partition.uid_start = 50000001
        partition.count = 10001
        partition.save()
        self.tick().assert_not_called()
        children = list(run.partitions.filter(state="pending").order_by("uid_start"))
        self.assertEqual([(child.uid_start, child.uid_end) for child in children],
            [(50000001, 100000002), (100000003, 9999999999)])
        self.assertEqual(collections.partition_uid_filter(children[-1]), ("1:100000002[UID]", True))

    def test_real_silent_range_collapse_cannot_mark_zero_results_complete(self):
        # Actual ESearch returned this querytranslation and count=0 after being
        # sent 40000000:9999999999[UID]. It silently discarded the upper bound.
        collapsed = '("Neoplasms"[MeSH Terms] OR "cancer*"[Title/Abstract]) AND 40000000[UID] AND 2026/01/01[Date - Publication]'
        run = self.ready()
        partition = run.partitions.get()
        partition.start_date = partition.end_date
        partition.uid_start = 40000000
        partition.count = 2
        partition.save()
        self.tick(search_result(0, translation=collapsed))
        run.refresh_from_db()
        partition.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertIn("检索翻译", run.message)
        self.assertEqual((partition.state, partition.count, partition.offset, partition.pmids), ("pending", 2, 0, []))
        self.assertIsNone(partition.captured_at)
        self.assertFalse(run.entries.exists())

    def test_uid_translation_requires_full_bounds_and_correct_boolean_direction(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.uid_start, partition.uid_end = 100, 200
        for translation in (None, "cancer AND 100[UID]", "cancer AND 1100:200[UID]",
            "cancer NOT 100:200[UID]", "cancer OR 100:200[UID]", "cancer AND 100:200[UID] AND 123[UID]"):
            with self.subTest(translation=translation), self.assertRaises(SourceBlocked):
                collections.validate_uid_translation({"querytranslation": translation}, partition)
        collections.validate_uid_translation({"querytranslation": "cancer AND (( 100 : 200 [ uid ] ))"}, partition)
        partition.uid_start = partition.uid_end = 123
        for translation in ("cancer AND 123[UID]", "cancer AND 123:123[UID]"):
            collections.validate_uid_translation({"querytranslation": translation}, partition)
        partition.uid_start, partition.uid_end = 50000001, collections.MAX_PUBMED_UID
        for translation in ("cancer AND 1:50000000[UID]", "cancer NOT 1[UID]", "cancer NOT 1:49999999[UID]"):
            with self.subTest(translation=translation), self.assertRaises(SourceBlocked):
                collections.validate_uid_translation({"querytranslation": translation}, partition)
        collections.validate_uid_translation({"querytranslation": "cancer NOT ((1:50000000[UID]))"}, partition)

    def test_uid_search_groups_original_query_and_leaves_full_range_unchanged(self):
        run = self.ready()
        partition = run.partitions.get()
        http = MagicMock()
        http.fetch.return_value = search_result(2, ["123", "124"])
        collections.search(http, run, partition, 9999)
        original = parse_qs(urlparse(http.fetch.call_args.args[0]).query)
        self.assertEqual(original["term"], [run.query])
        partition.uid_start, partition.uid_end = 100, 200
        http.fetch.return_value = search_result(2, ["123", "124"], translation="cancer AND ((100:200[uid]))")
        collections.search(http, run, partition, 9999)
        sliced = parse_qs(urlparse(http.fetch.call_args.args[0]).query)
        self.assertEqual(sliced["term"], [f"({run.query}) AND (100:200[UID])"])
        for field in ("mindate", "maxdate", "datetype", "sort", "retmax"):
            self.assertEqual(sliced[field], original[field])
        partition.uid_start, partition.uid_end = 50000001, collections.MAX_PUBMED_UID
        http.fetch.return_value = search_result(0, translation="cancer NOT (1:50000000[UID])")
        collections.search(http, run, partition)
        tail = parse_qs(urlparse(http.fetch.call_args.args[0]).query)
        self.assertEqual(tail["term"], [f"({run.query}) NOT (1:50000000[UID])"])
        self.assertNotIn("9999999999", tail["term"][0])

    def test_invalid_or_out_of_range_uid_does_not_advance_checkpoint(self):
        run = self.ready()
        partition = run.partitions.get()
        partition.uid_start, partition.uid_end = 100, 200
        partition.save()
        for ids in (["99"], ["201"], ["0"], ["0123"], ["１２３"], [123], [None], [["123"]], ["123", "123"]):
            with self.subTest(ids=ids):
                self.tick(search_result(len(ids), ids, translation="cancer AND 100:200[UID]"))
                run.refresh_from_db()
                partition.refresh_from_db()
                self.assertEqual(run.state, "paused")
                self.assertEqual((partition.state, partition.offset, partition.pmids), ("pending", 0, []))
                self.assertFalse(run.entries.exists())
                collections.control_collection(run.pk, "resume")

    @patch.object(collections, "PUBMED_SLICE_LIMIT", 3)
    def test_uid_children_pause_resume_capture_all_ids_once_and_preserve_offsets(self):
        run = self.ready(max_items=10, max_fulltexts=0, page_size=1)
        partition = run.partitions.get()
        partition.start_date = partition.end_date
        partition.uid_start, partition.uid_end, partition.count = 100, 105, 6
        partition.save()
        self.tick().assert_not_called()  # Known overflow splits without another request.
        self.tick(search_result(3, translation="cancer AND 103:105[UID]"))
        self.tick(search_result(3, ["105", "104", "103"], translation="cancer AND 103:105[UID]"))
        self.tick().assert_not_called()
        high = run.partitions.get(uid_start=103, uid_end=105)
        self.assertEqual((high.count, high.offset, high.state), (3, 1, "captured"))
        snapshot = list(run.partitions.order_by("pk").values("pk", "state", "count", "offset", "pmids", "uid_start", "uid_end"))
        collections.control_collection(run.pk, "pause")
        self.tick().assert_not_called()
        collections.control_collection(run.pk, "resume")
        self.assertEqual(snapshot, list(run.partitions.order_by("pk").values("pk", "state", "count", "offset", "pmids", "uid_start", "uid_end")))
        self.tick(articles(article("105")))
        for pmid in ("104", "103"):
            self.tick().assert_not_called()
            self.tick(articles(article(pmid)))
        self.tick(search_result(3, translation="cancer AND 100:102[UID]"))
        self.tick(search_result(3, ["102", "101", "100"], translation="cancer AND 100:102[UID]"))
        for pmid in ("102", "101", "100"):
            self.tick().assert_not_called()
            self.tick(articles(article(pmid)))
        self.tick().assert_not_called()  # All disjoint slices are exhausted.
        self.tick().assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, "complete")
        self.assertEqual(set(run.entries.values_list("pmid", flat=True)), {str(pmid) for pmid in range(100, 106)})
        self.assertEqual(run.entries.count(), 6)
        self.assertEqual(run.entries.filter(metadata_version__isnull=False).count(), 6)
        self.assertEqual(run.partitions.filter(state="done").count(), 2)
        self.assertEqual(run.partitions.filter(state="split").count(), 1)

    def test_batched_metadata_pause_resume_and_cross_run_deduplication(self):
        run = self.ready(max_items=1, max_fulltexts=0)
        self.seed_ids(run, ["123", "124"])
        self.tick()  # Persist exactly one pending PMID before network I/O.
        collections.control_collection(run.pk, "pause")
        self.tick().assert_not_called()
        collections.control_collection(run.pk, "resume")
        self.tick(articles(article()))
        self.tick()  # Enter fulltext phase.
        self.tick()  # Stop at the wave budget.
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.entries.count(), 1)
        version = MaterialVersion.objects.get()
        self.assertEqual(
            LiteratureClassification.objects.get().result["scope"], "mixed_candidate"
        )
        collections.control_collection(run.pk, "resume", {"max_items": 2})
        self.tick()
        self.tick(articles(article("124", title="Unknown publication")))
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, "complete")
        other = self.ready(max_items=1, max_fulltexts=0)
        self.seed_ids(other, ["123"])
        self.tick()
        self.tick().assert_not_called()
        self.assertEqual(Material.objects.count(), 2)
        self.assertEqual(other.entries.get().metadata_version_id, version.pk)
        self.assertTrue(other.entries.get().reused)

    def test_missing_ids_does_not_advance_or_silently_drop_entries(self):
        run = self.ready(max_items=2)
        self.seed_ids(run, ["123", "124"])
        self.tick()
        self.tick(articles(article()))
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.entries.filter(metadata_version__isnull=True).count(), 2)
        self.assertFalse(Material.objects.exists())
        collections.control_collection(run.pk, "resume")
        self.tick(articles(article(), article("124")))
        self.assertEqual(run.entries.filter(metadata_version__isnull=False).count(), 2)

    def test_lowering_limits_cannot_overbook_persisted_pending_pmids(self):
        run = self.ready(max_items=2)
        self.seed_ids(run, ["123", "124"])
        self.tick()
        collections.control_collection(run.pk, "pause")
        with self.assertRaisesMessage(SourceBlocked, "待下载条目"):
            collections.control_collection(run.pk, "resume", {"max_items": 1})
        self.assertEqual(run.entries.count(), 2)

    def test_resume_reads_entry_budgets_once_with_covering_index(self):
        run = self.ready(max_items=2, max_fulltexts=5)
        self.seed_ids(run, ["123", "124"])
        self.tick()
        self.tick(articles(article(pmc="PMC123"), article("124", "PMC124")))
        collections.control_collection(run.pk, "pause")
        with CaptureQueriesContext(connection) as captured:
            resumed = collections.control_collection(run.pk, "resume")
        self.assertEqual(resumed.state, "running")
        entry_queries = [
            query["sql"] for query in captured.captured_queries
            if 'FROM "curation_collectionentry"' in query["sql"]
        ]
        self.assertEqual(len(entry_queries), 1, entry_queries)
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + entry_queries[0])
            plan = [row[3] for row in cursor.fetchall()]
        self.assertTrue(
            any("COVERING INDEX collect_entry_fulltext" in row for row in plan), plan
        )

    def test_resume_combined_counts_preserve_attempt_and_pending_guards(self):
        run = self.ready(max_items=4, max_fulltexts=4)
        self.seed_ids(run, ["123", "124", "125", "126"])
        self.tick()
        self.tick(articles(*(article(pmid, f"PMC{pmid}") for pmid in ("123", "124", "125", "126"))))
        collections.control_collection(run.pk, "pause")
        for pmid, state in (("123", "parsed"), ("124", "reused"), ("125", "unavailable")):
            run.entries.filter(pmid=pmid).update(fulltext_state=state)
        for limits, message in (
            ({"max_items": 3}, "待下载条目"),
            ({"max_fulltexts": 2}, "已经处理的全文尝试数"),
            ({"max_fulltexts": 3}, "已达到文献和全文上限"),
        ):
            with self.subTest(limits=limits), self.assertRaisesMessage(SourceBlocked, message):
                collections.control_collection(run.pk, "resume", limits)
        # A conflict counts toward the original non-pending double-cap guard,
        # but does not become an attempted fulltext download.
        run.entries.filter(pmid="126").update(fulltext_state="identity_conflict")
        with self.assertRaisesMessage(SourceBlocked, "已达到文献和全文上限"):
            collections.control_collection(run.pk, "resume", {"max_fulltexts": 4})
        run.entries.filter(pmid="126").update(fulltext_state="pending", metadata_version=None)
        resumed = collections.control_collection(run.pk, "resume", {"max_fulltexts": 3})
        self.assertEqual(resumed.state, "running")
        self.assertEqual(resumed.phase, "metadata")
        self.assertEqual(resumed.limits["max_fulltexts"], 3)

    def test_pending_fulltext_keeps_exhausted_metadata_snapshot_resumable(self):
        run = self.ready(max_items=1, max_fulltexts=0)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(
            collections.control_collection(
                run.pk, "resume", {"max_fulltexts": 1}
            ).state,
            "running",
        )

    def test_expanded_fulltext_limit_preserves_default_and_network_bounds(self):
        limits = collections.validated_limits({"max_fulltexts": 2000})
        self.assertEqual(limits["max_fulltexts"], 2000)
        self.assertEqual(limits["interval_seconds"], 2)
        self.assertEqual(limits["bytes_per_second"], 256 * 1024)
        self.assertEqual(collections.validated_limits({})["max_fulltexts"], 5)
        with self.assertRaises(SourceBlocked):
            collections.validated_limits({"max_fulltexts": 2001})

    def test_expanded_fulltexts_resume_pending_without_revisiting_metadata(self):
        run = self.ready(max_fulltexts=1)
        collections.control_collection(run.pk, "pause")
        collections.start_full_collection(run.pk, self.full_observation(run))
        self.seed_ids(run, ["123", "124"])
        self.tick()
        self.tick(articles(article(pmc="PMC123"), article("124", "PMC124")))
        self.tick()
        template = '<OAI><article><front><article-meta><article-id pub-id-type="pmc">{pmid}</article-id><article-id pub-id-type="pmid">{pmid}</article-id><permissions><license>CC BY 4.0</license></permissions></article-meta></front><body><p>Patients were included in this research article. The observed effects depend on the population and research design.</p></body></article></OAI>'
        self.tick(template.format(pmid="123").encode())
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.phase, "metadata")
        estimate = run.estimate
        started_at = run.started_at
        first_version = run.entries.get(pmid="123").fulltext_version_id
        self.assertIsNotNone(estimate["acquisition"]["baseline_completed_at"])
        self.assertIsNone(estimate["acquisition"]["incremental_watermark"])

        resumed = collections.control_collection(
            run.pk, "resume", {"max_fulltexts": 2000}
        )
        self.assertEqual(resumed.phase, "fulltext")
        self.assertEqual(resumed.started_at, started_at)
        self.assertEqual(resumed.limits["interval_seconds"], 2)
        self.assertEqual(resumed.limits["bytes_per_second"], 256 * 1024)
        with patch.object(collections, "collect_metadata") as metadata:
            fetched = self.tick(template.format(pmid="124").encode())
        metadata.assert_not_called()
        fetched.assert_called_once()
        self.assertIn("124", fetched.call_args.args[0])
        self.assertEqual(run.entries.get(pmid="123").fulltext_version_id, first_version)
        self.assertEqual(run.entries.get(pmid="124").fulltext_state, "parsed")
        self.tick().assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.state, "complete")
        self.assertEqual(run.estimate, estimate)

    def test_expanded_fulltext_budget_does_not_skip_pending_metadata_or_partitions(self):
        for metadata_pending in (True, False):
            with self.subTest(metadata_pending=metadata_pending):
                run = self.ready(max_fulltexts=0)
                self.seed_ids(run, ["123", "124"])
                if metadata_pending:
                    self.tick()
                collections.control_collection(run.pk, "pause")
                resumed = collections.control_collection(
                    run.pk, "resume", {"max_fulltexts": 2000}
                )
                self.assertEqual(resumed.phase, "metadata")
                collections.control_collection(run.pk, "pause")
                # Leave no queued step to interfere with the next subtest's fixture.
                self.tick().assert_not_called()

    def test_fulltext_cache_and_fetch_use_frozen_metadata_identity(self):
        run = self.ready(max_items=1, max_fulltexts=1)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        material = Material.objects.get()
        template = '<OAI><article><front><article-meta><article-id pub-id-type="pmc">{pmc}</article-id><article-id pub-id-type="pmid">123</article-id><permissions><license>CC BY 4.0</license></permissions></article-meta></front><body><p>Patients were included in this research article. The observed effects depend on the population and research design.</p></body></article></OAI>'
        old = MaterialVersion.objects.create(
            material=material,
            sha256=store_blob(template.format(pmc="999").encode()),
            media_type="application/jats+xml",
            filename="PMC999.xml",
            rights="CC BY 4.0",
        )
        material.identifiers = {"pubmed": "123", "pmc": "PMC999"}
        material.save()
        fetched = self.tick(template.format(pmc="123").encode())
        self.assertIn("123", fetched.call_args.args[0])
        self.assertNotIn("999", fetched.call_args.args[0])
        entry = run.entries.get()
        self.assertEqual(entry.fulltext_state, "parsed")
        self.assertNotEqual(entry.fulltext_version_id, old.pk)
        self.assertTrue(MaterialVersion.objects.filter(pk=old.pk).exists())

    def test_fulltext_is_licensed_parsed_and_does_not_enqueue_model_calls(self):
        run = self.ready(max_items=1, max_fulltexts=1)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        fulltext = b'<OAI><article><front><article-meta><article-id pub-id-type="pmc">123</article-id><article-id pub-id-type="pmid">123</article-id><permissions><license>CC BY 4.0</license></permissions></article-meta></front><body><p>Patients were included in this illustrative research article. The observed effects depend on the population and research design.</p></body></article></OAI>'
        self.tick(fulltext)
        self.assertEqual(run.entries.get().fulltext_state, "parsed")
        self.assertTrue(Passage.objects.exists())
        self.assertFalse(WorkItem.objects.exclude(kind="collect_literature").exists())

    def test_pmc_identity_failure_is_recorded_without_false_fulltext_success(self):
        run = self.ready(max_items=1, max_fulltexts=1)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article(pmc="PMC123")))
        self.tick()
        self.tick(b'<OAI><error code="cannotDisseminateFormat"/></OAI>')
        self.assertEqual(run.entries.get().fulltext_state, "unavailable")
        self.assertFalse(Passage.objects.exists())

    def test_retry_after_is_persistent_and_six_failures_pause(self):
        run = self.create()
        for index in range(6):
            self.tick(effect=CollectionRetry("HTTP 429", retry_after=600))
            run.refresh_from_db()
            if index < 5:
                job = WorkItem.objects.get(state="queued")
                self.assertGreaterEqual(
                    (job.available_at - timezone.now()).total_seconds(), 599
                )
                WorkItem.objects.filter(pk=job.pk).update(available_at=timezone.now())
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.consecutive_errors, 6)
        self.assertIsNone(workflows.claim())

    def test_scheduled_source_window_preserves_failures_without_triggering_pause(self):
        run = self.create()
        run.consecutive_errors = 5
        run.save(update_fields=["consecutive_errors"])
        for delay in (7200, 60):
            self.tick(
                effect=CollectionRetry(
                    "PMC 批量请求等待非高峰时段",
                    retry_after=delay,
                    scheduling_wait=True,
                )
            )
            run.refresh_from_db()
            self.assertEqual(run.state, "estimating")
            self.assertEqual(run.consecutive_errors, 5)
            self.assertGreaterEqual(
                (run.retry_not_before - timezone.now()).total_seconds(), 7190
            )
            self.assertFalse(run.alerts.exists())
            self.assertIn("自动继续", run.message)
            self.assertNotIn("连续失败", run.message)
            job = WorkItem.objects.get(state="queued")
            self.assertGreaterEqual(job.available_at, run.retry_not_before)
            self.assertIsNone(workflows.claim())
            WorkItem.objects.filter(pk=job.pk).update(available_at=timezone.now())
        # A subsequent genuine sixth failure still pauses; the window did not
        # erase the prior failure history or falsely count as a successful request.
        self.tick(effect=CollectionRetry("HTTP 503", retry_after=60))
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(run.consecutive_errors, 6)
        self.assertEqual(run.alerts.get().reason, "retry_exhausted")

    def test_claim_recovery_replays_same_step_without_duplicate_rows(self):
        run = self.ready(max_items=1)
        self.seed_ids(run, ["123"])
        self.tick()
        old = workflows.claim()
        WorkItem.objects.filter(pk=old.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNone(workflows.claim())
        queued = WorkItem.objects.get(state="queued")
        self.assertNotEqual(queued.pk, old.pk)
        WorkItem.objects.filter(pk=queued.pk).update(available_at=timezone.now())
        current = workflows.claim()
        with patch.object(CollectionHTTP, "fetch", return_value=articles(article())):
            workflows.execute(current)
        # Expired process cannot overwrite the committed checkpoint.
        self.assertFalse(workflows.finish(old, "failed", message="stale"))
        self.assertEqual(run.entries.count(), 1)
        self.assertEqual(Material.objects.count(), 1)

    def test_taxonomy_versioning_unknown_and_multi_label_provenance(self):
        run = self.ready(max_items=1)
        self.seed_ids(run, ["123"])
        self.tick()
        self.tick(articles(article()))
        first = LiteratureClassification.objects.get()
        ids = {label["id"] for label in first.result["labels"]}
        self.assertTrue({"lung", "breast", "hematologic", "randomized_trial"} <= ids)
        self.assertEqual(first.result["review_status"], "unreviewed")
        new = taxonomy_snapshot()
        new["version"] = "test-v2"
        second = classify(first.version, {"title": "An unrelated document"}, new)
        self.assertEqual(second.result["scope"], "uncertain")
        self.assertEqual(LiteratureClassification.objects.count(), 2)

    def test_budget_reservation_http_backoff_and_byte_measurement(self):
        run = self.create(max_requests=2)
        job = workflows.claim()
        response = MagicMock()
        data = gzip.compress(b'{"example":123}')
        response.read.side_effect = io.BytesIO(data).read
        response.headers = {"Content-Encoding": "gzip"}
        response.status = 200
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with (
            patch("apps.curation.collection_network.build_opener", return_value=opener),
            patch("apps.curation.collection_network.time.sleep"),
        ):
            self.assertEqual(
                CollectionHTTP(job).fetch("https://eutils.ncbi.nlm.nih.gov/a"),
                b'{"example":123}',
            )
            receipt = run.fetches.get()
            self.assertEqual(receipt.wire_bytes, len(data))
            self.assertEqual(receipt.decoded_bytes, 15)
            self.assertEqual(receipt.charged_bytes, max(len(data), 15))
            opener.open.side_effect = HTTPError(
                "", 429, "", {"Retry-After": "600"}, None
            )
            with self.assertRaises(CollectionRetry) as caught:
                CollectionHTTP(job).fetch("https://eutils.ncbi.nlm.nih.gov/a")
            self.assertEqual(caught.exception.retry_after, 600)
            with self.assertRaises(CollectionStopped):
                CollectionHTTP(job).fetch("https://eutils.ncbi.nlm.nih.gov/a")
            self.assertEqual(opener.open.call_count, 2)

    def test_orphan_reservation_counts_toward_budget_and_pause_blocks_requests(self):
        run = self.create(max_bytes=1024**2)
        job = workflows.claim()
        CollectionFetch.objects.create(
            collection=run,
            url="https://eutils.ncbi.nlm.nih.gov/a",
            charged_bytes=1024**2,
        )
        with (
            patch("apps.curation.collection_network.build_opener") as opener,
            patch("apps.curation.collection_network.time.sleep"),
        ):
            with self.assertRaises(CollectionStopped):
                CollectionHTTP(job).fetch("https://eutils.ncbi.nlm.nih.gov/a")
            collections.control_collection(run.pk, "pause")
            with self.assertRaises(CollectionStopped):
                CollectionHTTP(job).fetch("https://eutils.ncbi.nlm.nih.gov/a")
            opener.assert_not_called()

    def test_api_csrf_and_estimate_response(self):
        data = {"start_date": "2021-10-15", "end_date": "2026-10-15"}
        secure = Client(enforce_csrf_checks=True)
        self.assertEqual(
            secure.post(
                "/api/v1/curation/collections/", data, content_type="application/json"
            ).status_code,
            403,
        )
        response = self.client.post(
            "/api/v1/curation/collections/", data, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["state"], "estimating")
        again = self.client.post(
            "/api/v1/curation/collections/", data, content_type="application/json"
        )
        self.assertEqual(again.status_code, 400)

    def test_minimal_runtime_without_timezone_database_uses_conservative_hours(self):
        with patch(
            "apps.curation.collection_network.ZoneInfo",
            side_effect=ZoneInfoNotFoundError,
        ):
            self.assertFalse(pmc_peak_hours(timezone.now().replace(hour=3)))
            self.assertTrue(pmc_peak_hours(timezone.now().replace(hour=15)))

    def test_unexpected_worker_error_pauses_collection_with_checkpoint_retained(self):
        run = self.create()
        job = workflows.claim()
        with patch.object(
            CollectionHTTP, "fetch", side_effect=RuntimeError("runtime failure")
        ):
            workflows.execute(job)
        run.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(run.state, "paused")
        self.assertEqual(job.state, "failed")
        self.assertFalse(Material.objects.exists())
        self.assertEqual(
            collections.control_collection(run.pk, "resume").phase, "estimate"
        )
