from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.annotations.engines.base import EngineFailed
from apps.annotations.engines import transcript_prefer
from apps.annotations.engines.vep import _load_json_lines
from apps.annotations.knowledge import store as knowledge_store
from apps.annotations.models import AnnotationJob
from apps.annotations.services import jobs as jobs_service


class CooldownRegressionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def tearDown(self):
        cache.clear()

    @override_settings(BIOTOOLS_JOB_COOLDOWN_SECONDS=10)
    def test_default_cache_backend_does_not_require_ttl(self):
        request = self.factory.post("/api/v1/jobs/", REMOTE_ADDR="127.0.0.1")
        jobs_service.check_submit_cooldown(request)
        with self.assertRaises(jobs_service.JobSubmitTooSoon) as context:
            jobs_service.check_submit_cooldown(request)
        self.assertGreaterEqual(context.exception.retry_after, 1)
        self.assertLessEqual(context.exception.retry_after, 10)

    @override_settings(BIOTOOLS_TRUST_X_FORWARDED_FOR=False)
    def test_forwarded_for_is_ignored_without_trusted_proxy_opt_in(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="10.0.0.10",
            HTTP_X_FORWARDED_FOR="203.0.113.50, 10.0.0.2",
        )
        self.assertEqual(jobs_service._client_ident(request), "ip:10.0.0.10")

    @override_settings(BIOTOOLS_TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_for_first_hop_used_after_explicit_opt_in(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="10.0.0.10",
            HTTP_X_FORWARDED_FOR="203.0.113.50, 10.0.0.2",
        )
        self.assertEqual(jobs_service._client_ident(request), "ip:203.0.113.50")


class KnowledgeStoreRegressionTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "meta.json").write_text(
            json.dumps({"imported_at": "2026-08-01T00:00:00Z"}),
            encoding="utf-8",
        )
        genes = [
            {
                "gene_id": 7157,
                "symbol": "TP53",
                "name": "tumor protein p53",
                "type": "protein-coding",
                "ensembl_gene": "ENSG00000141510",
                "hgnc_id": "HGNC:11998",
                "synonyms": ["P53"],
            },
            {
                "gene_id": 672,
                "symbol": "BRCA1",
                "name": "BRCA1 DNA repair associated",
                "type": "protein-coding",
                "ensembl_gene": "ENSG00000012048",
                "synonyms": ["RNF53"],
            },
        ]
        (root / "genes.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in genes),
            encoding="utf-8",
        )
        transcripts = {
            "records": {
                "NM_000546": {
                    "gene": "TP53",
                    "gene_id": "7157",
                    "refseq": ["NM_000546.6"],
                    "ensembl": ["ENST00000269305.9"],
                    "mane": True,
                    "mane_status": "select",
                },
                "NM_007294": {
                    "gene": "BRCA1",
                    "gene_id": "672",
                    "refseq": ["NM_007294.4"],
                    "ensembl": ["ENST00000357654.9"],
                    "mane": True,
                    "mane_status": "select",
                },
            }
        }
        (root / "transcript-map.json").write_text(
            json.dumps(transcripts), encoding="utf-8"
        )
        self.settings_override = override_settings(KNOWLEDGE_DIR=self.tmp.name)
        self.settings_override.enable()
        knowledge_store.reload()
        self.client = Client()

    def tearDown(self):
        self.settings_override.disable()
        self.tmp.cleanup()
        knowledge_store.reload()

    def test_meta_and_indexed_gene_search(self):
        meta = knowledge_store.get_meta()
        self.assertTrue(meta["ready"])
        self.assertEqual(meta["gene_loaded"], 2)
        result = knowledge_store.search_genes("TP", limit=10)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["symbol"], "TP53")

    def test_transcript_lookup_normalizes_missing_underscore_and_version(self):
        result = knowledge_store.lookup_transcript("nm000546.6")
        self.assertIsNotNone(result)
        self.assertEqual(result["accession"], "NM_000546")
        self.assertEqual(result["mane_status"], "select")

    def test_transcript_search_uses_alias_index(self):
        result = knowledge_store.search_transcripts("NM000546.6", mane_only=True)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["gene"], "TP53")

    def test_knowledge_views_bound_invalid_pagination(self):
        response = self.client.get(
            "/api/v1/knowledge/genes/?q=TP&limit=invalid&offset=-10"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(payload["results"][0]["symbol"], "TP53")


class VepOutputRegressionTests(SimpleTestCase):
    def test_malformed_json_line_becomes_engine_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.json"
            path.write_text('{"input":"ok"}\nnot-json\n', encoding="utf-8")
            with self.assertRaises(EngineFailed) as context:
                _load_json_lines(path)
        self.assertIn("line 2", str(context.exception))


class TranscriptPreferenceRegressionTests(SimpleTestCase):
    def test_local_mane_record_is_used(self):
        with patch(
            "apps.annotations.engines.transcript_prefer.knowledge_store.lookup_transcript",
            return_value={"mane": True, "mane_status": "select"},
        ):
            rank = transcript_prefer.mane_rank_from_accession("NM_000546.6")
        self.assertEqual(rank, 0)


class LegacyViewRegressionTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(BIOTOOLS_API_KEYS="", BIOTOOLS_REQUIRE_API_KEY=False)
    @patch("apps.annotations.api.views.annotate_service.annotate")
    def test_legacy_annotation_success_shape(self, annotate):
        annotate.return_value = {
            "assembly": "GRCh37",
            "engine": "vep",
            "results": [{"input": "17:7577120 C>T"}],
        }
        response = self.client.post(
            "/api/v1/legacy/annotate",
            data={"assembly": "GRCh37", "variants": ["17:7577120 C>T"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(response.json()["results"]), 1)


class DurableWorkerRegressionTests(TestCase):
    def test_job_result_keeps_version_when_snapshot_changes_during_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            updates = root / "updates"
            release = "a" * 32
            replacement = updates / "releases" / release
            for directory, version in ((baseline, "initial"), (replacement, release)):
                directory.mkdir(parents=True)
                (directory / "meta.json").write_text(json.dumps({"release": version}))
            with override_settings(KNOWLEDGE_DIR=str(baseline), KNOWLEDGE_UPDATES_DIR=str(updates)):
                knowledge_store.reload()
                job = jobs_service.create_and_enqueue(
                    variants=["17:7577120 C>T"], assembly="GRCh37", engines=["vep"]
                )

                def annotate(**kwargs):
                    (updates / "current.json").write_text(json.dumps({"release": release}))
                    knowledge_store.reload()
                    return {"results": [{"input": "17:7577120 C>T"}]}

                with patch.object(jobs_service.annotate_service, "annotate", side_effect=annotate):
                    jobs_service.run_job_safe(jobs_service.claim_next_job())
                job.refresh_from_db()
                self.assertEqual(job.status, AnnotationJob.Status.SUCCEEDED)
                self.assertEqual(job.result["knowledge_version"]["release"], "initial")
                self.assertEqual(knowledge_store.data_version()["release"], release)

    def test_claim_and_recover_stale_job(self):
        job = AnnotationJob.objects.create(
            assembly="GRCh37",
            engines=["vep"],
            variants=["17:7577120 C>T"],
            variant_count=1,
        )
        claimed = jobs_service.claim_next_job()
        self.assertEqual(claimed, str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, AnnotationJob.Status.RUNNING)
        self.assertIsNotNone(job.started_at)

        AnnotationJob.objects.filter(pk=job.pk).update(
            started_at=timezone.now() - timedelta(minutes=5)
        )
        recovered = jobs_service.recover_stale_jobs(60)
        self.assertEqual(recovered, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, AnnotationJob.Status.QUEUED)
        self.assertIsNone(job.started_at)

    @patch("apps.annotations.services.jobs.annotate_service.annotate")
    def test_claimed_job_reaches_terminal_success(self, annotate):
        annotate.return_value = {
            "assembly": "GRCh37",
            "engine": "vep",
            "results": [{"input": "17:7577120 C>T"}],
        }
        job = AnnotationJob.objects.create(
            assembly="GRCh37",
            engines=["vep"],
            variants=["17:7577120 C>T"],
            variant_count=1,
        )
        claimed = jobs_service.claim_next_job()
        self.assertEqual(claimed, str(job.id))
        jobs_service.run_job_safe(claimed)
        job.refresh_from_db()
        self.assertEqual(job.status, AnnotationJob.Status.SUCCEEDED)
        self.assertEqual(job.result["runs"][0]["status"], "ok")


class SettingsCompatibilityTests(SimpleTestCase):
    def test_database_path_preserves_current_and_legacy_configuration(self):
        for current, legacy, expected in (
            ("/tmp/current.sqlite3", "/tmp/legacy.sqlite3", "/tmp/current.sqlite3"),
            (None, "/tmp/legacy.sqlite3", "/tmp/legacy.sqlite3"),
        ):
            env = {key: value for key, value in os.environ.items()
                   if key not in {"DATABASE_PATH", "DJANGO_DB_PATH"}}
            env.update(DJANGO_DEBUG="1", DJANGO_DB_PATH=legacy)
            if current:
                env["DATABASE_PATH"] = current
            result = subprocess.check_output(
                [sys.executable, "-c", "from config.settings import DATABASES; print(DATABASES['default']['NAME'])"],
                env=env, text=True,
            )
            self.assertEqual(result.strip(), expected)

    def test_non_debug_startup_requires_a_secret(self):
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SECRET_KEY"}
        env["DJANGO_DEBUG"] = "0"
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings"], env=env, text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY is required", result.stderr)
