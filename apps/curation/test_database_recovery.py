import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.db import OperationalError, connection
from django.db.models import Q
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.annotations.management.commands.run_annotation_worker import Command
from apps.annotations.services import jobs
from apps.curation import collections, materials, workflows
from apps.curation.collection_network import CollectionHTTP, CollectionStopped
from apps.curation.collection_recovery import current_job
from apps.curation.db import LowerIdentifier
from apps.curation.models import Material, MaterialVersion
from apps.curation.test_collections import article, articles, search_result
from config.database import safe_failure_location, transient_database_error


class DatabaseRecoveryTests(TransactionTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = override_settings(CURATION_STORAGE_DIR=Path(directory.name))
        settings.enable()
        self.addCleanup(settings.disable)

    def tick(self, response):
        job = workflows.claim()
        self.assertIsNotNone(job)
        with patch.object(CollectionHTTP, "fetch", return_value=response):
            workflows.execute(job)
        return job

    def ready(self):
        run = collections.create_collection("2021-10-15", "2026-10-15")
        self.tick(search_result(2))
        collections.control_collection(run.pk, "resume")
        partition = run.partitions.get()
        partition.state, partition.pmids, partition.count = (
            "captured",
            ["123", "456"],
            2,
        )
        partition.save()
        self.tick(None)
        return run

    def test_partial_page_lock_rolls_back_and_retries_same_ids(self):
        run = self.ready()
        original = materials.save_prepared_pubmed_article
        calls = 0

        def save(prepared, url):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OperationalError("database is locked")
            return original(prepared, url)

        response = articles(article(), article("456"))
        with patch.object(
            collections, "save_prepared_pubmed_article", side_effect=save
        ):
            self.tick(response)
        run.refresh_from_db()
        self.assertEqual(run.state, "running")
        self.assertEqual(run.consecutive_errors, 1)
        self.assertEqual(MaterialVersion.objects.count(), 0)
        self.assertEqual(run.entries.filter(metadata_version__isnull=True).count(), 2)
        self.assertFalse(run.alerts.exists())
        due = current_job(run).available_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=due):
            self.tick(response)
        run.refresh_from_db()
        self.assertEqual(run.consecutive_errors, 0)
        self.assertEqual(run.entries.filter(metadata_version__isnull=False).count(), 2)
        self.assertEqual(MaterialVersion.objects.count(), 2)
        self.assertEqual(run.limits["interval_seconds"], 2)
        self.assertEqual(run.limits["bytes_per_second"], 262144)

    def test_blob_writes_and_classification_do_not_hold_write_transaction(self):
        self.ready()
        original_store = materials.store_blob
        original_classify = collections.classify_metadata

        def store(content):
            self.assertFalse(connection.in_atomic_block)
            return original_store(content)

        def classify(*args):
            self.assertFalse(connection.in_atomic_block)
            return original_classify(*args)

        with (
            patch.object(materials, "store_blob", side_effect=store),
            patch.object(collections, "classify_metadata", side_effect=classify),
        ):
            self.tick(articles(article(), article("456")))
        self.assertEqual(MaterialVersion.objects.count(), 2)

    def test_inode_exhaustion_stops_before_network_request(self):
        self.ready()
        job = workflows.claim()
        with (
            patch(
                "apps.curation.collection_network.os.statvfs",
                return_value=SimpleNamespace(
                    f_files=10000,
                    f_favail=3,
                    f_bavail=10000000,
                    f_frsize=4096,
                    f_blocks=10000000,
                    f_bfree=10000000,
                ),
            ),
            patch("apps.curation.collection_network.build_opener") as opener,
            self.assertRaisesMessage(CollectionStopped, "inode"),
        ):
            CollectionHTTP(job).fetch(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=123"
            )
        opener.assert_not_called()

    def test_idle_annotation_poll_does_not_start_write_transaction(self):
        with CaptureQueriesContext(connection) as capture:
            self.assertIsNone(jobs.claim_next_job())
            self.assertEqual(jobs.recover_stale_jobs(60), 0)
        self.assertFalse(any(q["sql"].startswith(("BEGIN", "UPDATE")) for q in capture))

    def test_identifier_collision_query_uses_both_indexes(self):
        plan = (
            Material.objects.annotate(
                doi_lower=LowerIdentifier("doi"), pmc_lower=LowerIdentifier("pmc")
            )
            .filter(Q(doi_lower__in=["10.1/test"]) | Q(pmc_lower__in=["pmc1"]))
            .explain()
        )
        self.assertIn("curation_doi_lookup", plan)
        self.assertIn("curation_pmc_lookup", plan)

    def test_annotation_poll_survives_temporary_database_failure(self):
        with (
            patch.object(jobs, "recover_stale_jobs", return_value=0),
            patch.object(
                jobs,
                "claim_next_job",
                side_effect=[OperationalError("database is locked"), None],
            ) as claim,
            patch(
                "apps.annotations.management.commands.run_annotation_worker.time.sleep"
            ),
        ):
            Command().handle(
                once=True, poll_interval=1, stale_after=60, recovery_interval=60
            )
        self.assertEqual(claim.call_count, 2)

    def test_only_known_temporary_errors_are_classified_for_recovery(self):
        self.assertTrue(
            transient_database_error(OperationalError("unable to open database file"))
        )
        self.assertFalse(
            transient_database_error(
                OperationalError("database disk image is malformed")
            )
        )
        self.assertNotIn(
            "secret", safe_failure_location(ValueError("secret document content"))
        )
