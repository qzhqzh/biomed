import io
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from defusedxml.common import EntitiesForbidden
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from pypdf import PdfWriter

from apps.curation import materials, review, workflows
from apps.curation.models import (
    Evidence,
    Material,
    MaterialVersion,
    Passage,
    Review,
    WorkAttempt,
    WorkItem,
)
from apps.curation.network import SourceBlocked, TransientSourceError, allowed, xml
from apps.curation.standards import mondo_terms
from apps.curation.storage import read_blob, store_blob


class CurationTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_context = override_settings(
            CURATION_STORAGE_DIR=Path(self.directory.name), CURATION_MODEL_BACKEND=""
        )
        self.settings_context.enable()
        self.addCleanup(self.settings_context.disable)
        self.material = Material.objects.create(
            source_key="test:article", title="Test source"
        )
        self.version = MaterialVersion.objects.create(
            material=self.material,
            sha256=store_blob(b"<article/>"),
            media_type="application/jats+xml",
            filename="test.xml",
            rights="CC BY",
        )
        self.passage = Passage.objects.create(
            version=self.version,
            parser_version=materials.PARSER_VERSION,
            ordinal=0,
            locator="body/p:1",
            text="In this study, the effect was uncertain in the specified population.",
        )

    def extraction(self):
        return workflows.enqueue(
            "extract",
            {
                "version_id": str(self.version.pk),
                "parser_version": materials.PARSER_VERSION,
            },
        )

    def candidate(self):
        return Evidence.objects.create(
            extraction=self.extraction(),
            ordinal=0,
            passage=self.passage,
            quote=self.passage.text,
            start=0,
            end=len(self.passage.text),
            assertion="The effect was uncertain.",
            context={"population": None, "study_type": None, "limitations": None},
        )

    def test_storage_content_addressed_and_tamper_detected(self):
        digest = store_blob(b"same content")
        self.assertEqual(store_blob(b"same content"), digest)
        self.assertEqual(read_blob(digest), b"same content")
        from apps.curation.storage import blob_path

        blob_path(digest).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            read_blob(digest)
        with self.assertRaises(ValueError):
            blob_path("../../secret")

    def test_queue_deduplication_claim_and_attempt_history(self):
        first = self.extraction()
        self.assertEqual(first.pk, self.extraction().pk)
        claimed = workflows.claim()
        self.assertEqual(claimed.pk, first.pk)
        self.assertIsNone(workflows.claim())
        workflows.execute(claimed)
        first.refresh_from_db()
        self.assertEqual(first.state, "waiting_configuration")
        self.assertEqual(
            WorkAttempt.objects.get(job=first).outcome, "waiting_configuration"
        )
        self.assertFalse(Evidence.objects.exists())

    def test_expired_lease_requeues_and_stale_finish_cannot_override(self):
        job = self.extraction()
        old = workflows.claim()
        WorkItem.objects.filter(pk=job.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        current = workflows.claim()
        self.assertEqual(current.pk, old.pk)
        self.assertNotEqual(current.lease_token, old.lease_token)
        self.assertFalse(workflows.finish(old, "succeeded", {"invalid": True}))
        self.assertTrue(workflows.finish(current, "succeeded", {"valid": True}))
        job.refresh_from_db()
        self.assertEqual(job.result, {"valid": True})
        self.assertEqual(
            list(job.runs.values_list("outcome", flat=True)),
            ["interrupted", "succeeded"],
        )

    def test_transient_failure_keeps_attempt_and_schedules_retry(self):
        self.extraction()
        claimed = workflows.claim()
        with patch(
            "apps.curation.workflows.dispatch",
            side_effect=TransientSourceError("temporary"),
        ):
            workflows.execute(claimed)
        claimed.refresh_from_db()
        self.assertEqual(claimed.state, "queued")
        self.assertGreater(claimed.available_at, timezone.now())
        self.assertEqual(claimed.runs.get().outcome, "retry")

    def test_parse_jats_idempotent_and_anchor_stable(self):
        raw = b"<OAI><article><front><article-meta><abstract><p>Abstract for the public study with its limitations clearly described.</p></abstract></article-meta></front><body><sec><p>The full source paragraph has a stable location and must be retained separately from its abstract.</p></sec></body></article></OAI>"
        version = MaterialVersion.objects.create(
            material=self.material,
            sha256=store_blob(raw),
            media_type="application/jats+xml",
            filename="paper.xml",
            rights="CC BY",
        )
        first = materials.parse_material(str(version.pk), materials.PARSER_VERSION)
        before = list(version.passages.values_list("id", "locator", "text"))
        second = materials.parse_material(str(version.pk), materials.PARSER_VERSION)
        self.assertEqual(first, second)
        self.assertEqual(
            before, list(version.passages.values_list("id", "locator", "text"))
        )
        self.assertEqual(before[-1][1], "body/p:1")
        self.assertEqual(WorkItem.objects.filter(kind="extract").count(), 1)

    def test_scanned_pdf_does_not_create_empty_evidence(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        material, version, _job = materials.upload_pdf(
            stream.getvalue(), "Scan", "guideline", "Legal copy"
        )
        with self.assertRaisesMessage(SourceBlocked, "OCR"):
            materials.parse_material(str(version.pk), materials.PARSER_VERSION)
        self.assertFalse(version.passages.exists())
        self.assertFalse(WorkItem.objects.filter(kind="extract").exists())
        self.assertEqual(material.kind, "guideline")

    def test_pdf_same_content_and_later_version_preserve_history(self):
        m1, v1, j1 = materials.upload_pdf(
            b"%PDF-1.4\nfirst", "Title", "guideline", "Legal copy"
        )
        m2, v2, j2 = materials.upload_pdf(
            b"%PDF-1.4\nfirst", "Title", "guideline", "Legal copy"
        )
        self.assertEqual((m1.pk, v1.pk, j1.pk), (m2.pk, v2.pk, j2.pk))
        _, later, _ = materials.upload_pdf(
            b"%PDF-1.4\nsecond", "Ignored title", "paper", "Legal copy", str(m1.pk)
        )
        self.assertNotEqual(later.pk, v1.pk)
        self.assertEqual(m1.versions.count(), 2)
        self.assertEqual(read_blob(v1.sha256), b"%PDF-1.4\nfirst")

    def test_material_categories_count_before_filter_and_pagination(self):
        Material.objects.create(
            source_key="guide:one", kind="guideline", title="Guide one"
        )
        Material.objects.create(
            source_key="guide:two", kind="guideline", title="Guide two"
        )
        Material.objects.create(source_key="other:one", kind="other", title="Other")
        response = self.client.get(
            "/api/v1/curation/materials/",
            {"kind": "guideline", "limit": 1, "offset": 1},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            result["counts"], {"all": 4, "paper": 1, "guideline": 2, "other": 1}
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["kind"], "guideline")
        empty = self.client.get("/api/v1/curation/materials/", {"q": "missing"}).json()
        self.assertEqual(
            empty["counts"], {"all": 0, "paper": 0, "guideline": 0, "other": 0}
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/materials/", {"kind": "unknown"}
            ).status_code,
            400,
        )

    def test_archive_pdf_preserves_original_and_creates_no_processing_job(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        raw = b"%PDF-1.4\narchival fixture"
        payload = {
            "title": "Guideline example",
            "kind": "guideline",
            "rights": "User-provided for archiving",
            "rights_confirmed": "true",
            "start_parse": "false",
        }
        response = self.client.post(
            "/api/v1/curation/materials/",
            {**payload, "file": SimpleUploadedFile("guide.pdf", raw)},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNone(data["job"])
        self.assertFalse(WorkItem.objects.exists())
        self.assertFalse(Evidence.objects.exists())
        version = MaterialVersion.objects.get(pk=data["version_id"])
        self.assertEqual(version.filename, "guide.pdf")
        self.assertEqual(version.material.kind, "guideline")
        self.assertEqual(read_blob(version.sha256), raw)
        detail = self.client.get(
            f"/api/v1/curation/materials/{version.material_id}/"
        ).json()
        self.assertIsNone(detail["parse_job"])
        self.assertEqual(detail["passage_total"], 0)
        downloaded = self.client.get(
            f"/api/v1/curation/versions/{version.pk}/download/"
        )
        self.assertEqual(b"".join(downloaded.streaming_content), raw)
        self.assertIn('filename="guide.pdf"', downloaded["Content-Disposition"])
        again = self.client.post(
            "/api/v1/curation/materials/",
            {**payload, "file": SimpleUploadedFile("guide.pdf", raw)},
        ).json()
        self.assertEqual(again["version_id"], data["version_id"])
        self.assertEqual(version.material.versions.count(), 1)
        self.assertFalse(WorkItem.objects.exists())

    def test_archive_option_preserves_existing_tasks_and_default_upload_behavior(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        raw = b"%PDF-1.4\npaper fixture"
        response = self.client.post(
            "/api/v1/curation/materials/",
            {
                "title": "Paper",
                "kind": "paper",
                "rights": "Legal copy",
                "rights_confirmed": "true",
                "file": SimpleUploadedFile("paper.pdf", raw),
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["job"]["state"], "queued")
        material, version, archive_job = materials.upload_pdf(
            raw,
            "Ignored",
            "guideline",
            "Legal copy",
            start_parse=False,
        )
        self.assertIsNone(archive_job)
        self.assertEqual(material.kind, "paper")
        self.assertEqual(WorkItem.objects.count(), 1)
        detail = self.client.get(f"/api/v1/curation/materials/{material.pk}/").json()
        self.assertEqual(detail["parse_job"]["id"], response.json()["job"]["id"])
        self.assertEqual(version.filename, "paper.pdf")

    def test_processing_view_of_archived_version_has_no_side_effects(self):
        _, version, _ = materials.upload_pdf(
            b"%PDF-1.4\narchived guide",
            "Guide",
            "guideline",
            "Local archive",
            filename="guide.pdf",
            start_parse=False,
        )
        url = f"/api/v1/curation/versions/{version.pk}/processing/"
        for _ in range(2):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["version_id"], str(version.pk))
            self.assertEqual(data["stages"], {"parse": None, "extract": None})
            self.assertEqual(data["results"], [])
            self.assertEqual(data["total"], 0)
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertFalse(WorkItem.objects.exists())
        self.assertFalse(WorkAttempt.objects.exists())
        self.assertFalse(version.passages.exists())

    def test_processing_history_is_version_scoped_and_latest_stages_ignore_pagination(
        self,
    ):
        from uuid import uuid4

        old = workflows.enqueue(
            "parse", {"version_id": str(self.version.pk), "parser_version": "old"}
        )
        WorkItem.objects.filter(pk=old.pk).update(state="failed", attempts=1)
        WorkAttempt.objects.create(
            job=old,
            number=1,
            token=uuid4(),
            outcome="failed",
            message="Example parse failure",
            finished_at=timezone.now(),
        )
        latest = workflows.enqueue(
            "parse", {"version_id": str(self.version.pk), "parser_version": "new"}
        )
        extraction = self.extraction()
        other = MaterialVersion.objects.create(
            material=self.material,
            sha256=store_blob(b"other version"),
            media_type="application/pdf",
            filename="other.pdf",
            rights="Local archive",
        )
        unrelated = workflows.enqueue("parse", {"version_id": str(other.pk)})
        response = self.client.get(
            f"/api/v1/curation/versions/{self.version.pk}/processing/?offset=2&limit=1"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["stages"]["parse"]["id"], str(latest.pk))
        self.assertEqual(data["stages"]["extract"]["id"], str(extraction.pk))
        self.assertEqual([job["id"] for job in data["results"]], [str(old.pk)])
        self.assertEqual(data["results"][0]["attempts"], 1)
        self.assertNotIn(str(unrelated.pk), response.content.decode())
        runs = self.client.get(f"/api/v1/curation/workflows/{old.pk}/").json()["runs"]
        self.assertEqual(runs[0]["message"], "Example parse failure")
        self.assertIsNotNone(runs[0]["finished_at"])
        self.assertEqual(WorkItem.objects.count(), 4)
        self.assertEqual(
            self.client.get(
                f"/api/v1/curation/versions/{uuid4()}/processing/"
            ).status_code,
            404,
        )

    def test_archive_does_not_bypass_rights_or_accept_unknown_parse_mode(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        for extra in (
            {"start_parse": "false"},
            {"rights_confirmed": "true", "start_parse": "maybe"},
        ):
            response = self.client.post(
                "/api/v1/curation/materials/",
                {
                    "title": "Guide",
                    "kind": "guideline",
                    "rights": "copy",
                    "file": SimpleUploadedFile("guide.pdf", b"%PDF-1.4"),
                    **extra,
                },
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(Material.objects.count(), 1)
        self.assertFalse(WorkItem.objects.exists())

    def test_pmc_without_license_or_mismatched_id_is_not_imported(self):
        for payload in [
            b"<OAI><error>Cannot disseminate</error></OAI>",
            b'<OAI><article><front><article-meta><article-id pub-id-type="pmc">1</article-id></article-meta></front><body><p>text</p></body></article></OAI>',
        ]:
            with (
                patch("apps.curation.materials.fetch", return_value=payload),
                self.assertRaises(SourceBlocked),
            ):
                materials.acquire_pmc(str(self.material.pk), "PMC1")
        self.assertEqual(self.material.versions.count(), 1)

    def test_pmc_license_saved_and_parse_queued(self):
        raw = b'<OAI><article><front><article-meta><article-id pub-id-type="pmc">PMC12</article-id><permissions><license>CC BY 4.0</license></permissions></article-meta></front><body><p>Source text.</p></body></article></OAI>'
        with patch("apps.curation.materials.fetch", return_value=raw):
            result = materials.acquire_pmc(str(self.material.pk), "PMC12")
        version = MaterialVersion.objects.get(pk=result["version_id"])
        self.assertIn("CC BY", version.rights)
        self.assertEqual(read_blob(version.sha256), raw)
        self.assertEqual(WorkItem.objects.get(pk=result["next_job"]).kind, "parse")

    def test_pubmed_metadata_does_not_become_full_text(self):
        raw = b'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>A real title</ArticleTitle><Abstract>abstract only</Abstract></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">123</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'
        with patch("apps.curation.materials.fetch", return_value=raw):
            first = materials.acquire_pubmed("123")
            second = materials.acquire_pubmed("123")
        self.assertEqual(first, second)
        material = Material.objects.get(pk=first["material_id"])
        self.assertEqual(material.versions.count(), 1)
        self.assertIsNone(first["next_job"])
        self.assertFalse(WorkItem.objects.exists())

    def test_forged_source_quote_is_rejected_before_candidate_saved(self):
        job = self.extraction()

        class Engine:
            def extract(self, passages):
                return [
                    {
                        "passage_id": str(passages[0]["id"]),
                        "start": 0,
                        "end": 6,
                        "quote": "forged",
                        "assertion": "wrong",
                        "context": {},
                    }
                ]

        with (
            patch(
                "apps.curation.review.provider",
                return_value=(Engine(), {"model": "test"}),
            ),
            self.assertRaises(SourceBlocked),
        ):
            review.extract_candidates(job)
        self.assertFalse(Evidence.objects.exists())
        self.assertFalse(Review.objects.exists())

    def test_unknown_entity_not_guessed(self):
        with self.assertRaises(SourceBlocked):
            review.resolve_entity({"namespace": "MONDO", "identifier": "MONDO:missing"})

    def test_ai_pass_publishes_without_human_but_rejection_stays_draft(self):
        candidate = self.candidate()
        provenance = {"model": "test", "policy_version": settings_policy()}
        job = workflows.enqueue(
            "review_ai", {"evidence_id": str(candidate.pk), "provenance": provenance}
        )

        class Engine:
            def review(self, value):
                return {"decision": "pass", "reason": "Test fixture, quote matches."}

        with patch(
            "apps.curation.review.provider", return_value=(Engine(), provenance)
        ):
            review.review_ai(job)
            review.review_ai(job)
        candidate.refresh_from_db()
        self.assertEqual(candidate.publication_status, "published")
        self.assertEqual(candidate.human_status, "unchecked")
        self.assertEqual(candidate.reviews.count(), 1)
        candidate2 = Evidence.objects.create(
            extraction=candidate.extraction,
            ordinal=1,
            passage=self.passage,
            quote=candidate.quote,
            start=0,
            end=candidate.end,
            assertion="Another assertion",
        )
        job2 = workflows.enqueue(
            "review_ai", {"evidence_id": str(candidate2.pk), "provenance": provenance}
        )
        with patch("apps.curation.review.provider") as provider:
            provider.return_value = (provider, provenance)
            provider.review.return_value = {
                "decision": "fail",
                "reason": "Not supported",
            }
            review.review_ai(job2)
        candidate2.refresh_from_db()
        self.assertEqual(candidate2.publication_status, "draft")
        self.assertEqual(candidate2.ai_status, "fail")

    def test_human_withdrawal_retains_audit_and_cannot_be_republished_by_retry(self):
        candidate = self.candidate()
        candidate.ai_status, candidate.publication_status = "pass", "published"
        candidate.save()
        user = get_user_model().objects.create_user(
            "reviewer", password="test-only-long-password"
        )
        user.user_permissions.add(
            Permission.objects.get(
                codename="change_evidence", content_type__app_label="curation"
            )
        )
        review.human_review(candidate.pk, user, "flag", "Incorrect population")
        candidate.refresh_from_db()
        self.assertEqual(candidate.publication_status, "withdrawn")
        self.assertEqual(candidate.human_status, "flagged")
        self.assertEqual(candidate.reviews.get().actor, user)
        with self.assertRaises(SourceBlocked):
            review.human_review(
                candidate.pk, user, "confirm", "Cannot overwrite withdrawal"
            )
        job = workflows.enqueue(
            "review_ai",
            {"evidence_id": str(candidate.pk), "provenance": {"model": "test"}},
        )
        with patch("apps.curation.review.provider") as provider:
            provider.return_value = (provider, {"model": "test"})
            provider.review.return_value = {
                "decision": "pass",
                "reason": "AI still passes",
            }
            review.review_ai(job)
        candidate.refresh_from_db()
        self.assertEqual(candidate.publication_status, "withdrawn")

    def test_review_requires_account_permission_and_nonempty_reason(self):
        candidate = self.candidate()
        response = self.client.post(
            f"/api/v1/curation/evidence/{candidate.pk}/",
            data={"decision": "confirm", "reason": "Checked"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(candidate.reviews.count(), 0)

    def test_mutations_require_csrf_and_cannot_forge_ai_publication(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            client.post(
                "/api/v1/curation/sources/",
                data={"namespace": "MONDO"},
                content_type="application/json",
            ).status_code,
            403,
        )
        client.get("/api/v1/curation/session/")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/v1/curation/evidence/",
            data={"ai_status": "pass", "publication_status": "published"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 405)
        self.assertFalse(Evidence.objects.exists())

    def test_pdf_upload_requires_explicit_rights(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            "/api/v1/curation/materials/",
            {
                "title": "Test",
                "kind": "paper",
                "rights": "copy",
                "file": SimpleUploadedFile("file.pdf", b"%PDF-1.4"),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Material.objects.count(), 1)

    def test_mondo_obsolete_excluded_and_relations_not_rewritten(self):
        raw = b'[Term]\nid: MONDO:1\nname: first\nsynonym: "Alias" EXACT []\nxref: OMIM:2\nis_a: MONDO:3 ! parent\n\n[Term]\nid: MONDO:9\nname: old\nis_obsolete: true\n'
        terms = mondo_terms(raw)
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0]["synonyms"], ["Alias"])
        self.assertEqual(terms[0]["xrefs"], ["OMIM:2"])
        self.assertEqual(terms[0]["parents"], ["MONDO:3"])

    def test_external_entities_and_arbitrary_hosts_rejected(self):
        for url in [
            "http://127.0.0.1/",
            "https://example.org/paper",
            "https://rxnav.nlm.nih.gov:444/",
            "https://user:password@rxnav.nlm.nih.gov/",
        ]:
            with self.assertRaises(SourceBlocked):
                allowed(url)
        with self.assertRaises(EntitiesForbidden):
            xml(
                b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>'
            )

    def test_formal_library_has_no_teaching_records(self):
        response = self.client.get("/api/v1/curation/evidence/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["results"], [])

    def test_interrupted_standard_staging_resumes_without_exposing_partial_release(
        self,
    ):
        from apps.curation.models import Concept, SourceRelease
        from apps.curation.standards import sync_standard

        old = SourceRelease.objects.create(
            namespace="MONDO",
            version="old",
            source_url="https://github.com/",
            rights="CC BY",
            sha256="a" * 64,
            count=1,
            active=True,
        )
        Concept.objects.create(
            release=old, identifier="MONDO:old", label="Old snapshot"
        )
        terms = [
            {
                "id": f"MONDO:{i}",
                "name": f"Disease {i}",
                "synonyms": [],
                "xrefs": [],
                "parents": [],
            }
            for i in range(10001)
        ]
        release = {
            "tag_name": "new",
            "assets": [
                {
                    "name": "mondo.obo",
                    "browser_download_url": "https://github.com/monarch-initiative/mondo/releases/download/new/mondo.obo",
                }
            ],
        }
        insert = Concept.objects.bulk_create
        count = 0

        def interrupted(rows, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("Simulated process interruption")
            return insert(rows, **kwargs)

        with (
            patch("apps.curation.standards.fetch_json", return_value=release),
            patch("apps.curation.standards.fetch", return_value=b"test-source"),
            patch("apps.curation.standards.mondo_terms", return_value=terms),
        ):
            with (
                patch.object(Concept.objects, "bulk_create", side_effect=interrupted),
                self.assertRaises(RuntimeError),
            ):
                sync_standard("MONDO")
            self.assertEqual(SourceRelease.objects.get(active=True).pk, old.pk)
            staged = SourceRelease.objects.exclude(pk=old.pk).get()
            self.assertEqual(staged.concepts.count(), 500)
            result = sync_standard("MONDO")
        self.assertEqual(result["count"], 10001)
        self.assertEqual(SourceRelease.objects.get(active=True).pk, staged.pk)
        self.assertEqual(staged.concepts.count(), 10001)
        self.assertEqual(old.concepts.count(), 1)

    def test_publication_integrity_is_enforced_by_database(self):
        from django.db import IntegrityError, transaction

        candidate = self.candidate()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Evidence.objects.filter(pk=candidate.pk).update(
                publication_status="published"
            )
        self.assertEqual(
            Evidence.objects.get(pk=candidate.pk).publication_status, "draft"
        )

    def test_extraction_checkpoint_survives_worker_completion_failure(self):
        job = self.extraction()
        claimed = workflows.claim()
        checkpoint = {
            "extraction_complete": True,
            "candidates": [],
            "count": 0,
            "provenance": {"model": "test"},
        }
        WorkItem.objects.filter(pk=job.pk).update(result=checkpoint)
        workflows.finish(claimed, "failed", message="Worker interrupted", retry=True)
        job.refresh_from_db()
        self.assertEqual(job.result, checkpoint)
        with patch(
            "apps.curation.review.provider",
            side_effect=AssertionError("Must not call model again"),
        ):
            self.assertEqual(review.extract_candidates(job), checkpoint)

    def test_reference_identifiers_cannot_override_article_identifiers(self):
        raw = b'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>Main article</ArticleTitle></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">123</ArticleId><ArticleId IdType="pmc">PMC12</ArticleId><ArticleId IdType="doi">10.test/main</ArticleId></ArticleIdList><ReferenceList><Reference><ArticleIdList><ArticleId IdType="pmc">PMC999</ArticleId><ArticleId IdType="doi">10.test/reference</ArticleId></ArticleIdList></Reference></ReferenceList></PubmedData></PubmedArticle></PubmedArticleSet>'
        with patch("apps.curation.materials.fetch", return_value=raw):
            result = materials.acquire_pubmed("123")
        material = Material.objects.get(pk=result["material_id"])
        self.assertEqual(material.identifiers["pmc"], "PMC12")
        self.assertEqual(material.identifiers["doi"], "10.test/main")
        self.assertEqual(
            WorkItem.objects.get(pk=result["next_job"]).payload["pmcid"], "PMC12"
        )

    def test_full_text_must_agree_with_article_pmid_and_doi(self):
        self.material.identifiers = {"pubmed": "123", "doi": "10.test/main"}
        self.material.save()
        for pmid, doi in [
            ("999", "10.test/main"),
            ("123", "10.test/reference"),
            ("", "10.test/main"),
        ]:
            raw = f'<OAI><article><front><article-meta><article-id pub-id-type="pmc">PMC12</article-id><article-id pub-id-type="pmid">{pmid}</article-id><article-id pub-id-type="doi">{doi}</article-id><permissions><license>CC BY</license></permissions></article-meta></front><body><p>Source text</p></body></article></OAI>'.encode()
            with (
                patch("apps.curation.materials.fetch", return_value=raw),
                self.assertRaises(SourceBlocked),
            ):
                materials.acquire_pmc(str(self.material.pk), "PMC12")
        self.assertEqual(self.material.versions.count(), 1)
        self.assertFalse(WorkItem.objects.exists())

    def test_quarantine_retains_source_but_blocks_evidence_and_default_selection(self):
        candidate = self.candidate()
        candidate.ai_status, candidate.publication_status = "pass", "published"
        candidate.save()
        job = candidate.extraction
        WorkItem.objects.filter(pk=job.pk).update(state="waiting_configuration")
        digest = self.version.sha256
        materials.quarantine_version(self.version.pk, "Source identifiers conflict")
        candidate.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(candidate.publication_status, "withdrawn")
        self.assertEqual(job.state, "blocked")
        self.assertEqual(read_blob(digest), b"<article/>")
        self.assertTrue(Passage.objects.filter(pk=self.passage.pk).exists())
        with self.assertRaises(SourceBlocked):
            review.extract_candidates(job)
        detail = self.client.get(
            f"/api/v1/curation/materials/{self.material.pk}/"
        ).json()
        self.assertIsNone(detail["selected_version"])
        self.assertEqual(detail["versions"][0]["integrity_status"], "quarantined")
        self.assertEqual(
            self.client.get("/api/v1/curation/evidence/").json()["total"], 0
        )

    def test_idle_worker_recovers_from_heartbeat_or_claim_lock(self):
        from django.core.management import call_command
        from django.db import OperationalError

        module = "apps.curation.management.commands.curation_worker"
        for failing_stage in ("heartbeat", "claim"):
            with (
                self.subTest(stage=failing_stage),
                patch(module + ".signal.signal"),
                patch(module + ".time.sleep") as sleep,
                patch(module + ".heartbeat") as beat,
                patch(module + ".claim") as claim,
            ):
                beat.side_effect = (
                    [OperationalError("database is locked"), True]
                    if failing_stage == "heartbeat"
                    else [True, True]
                )
                claim.side_effect = (
                    [OperationalError("database is locked"), None]
                    if failing_stage == "claim"
                    else [None]
                )
                call_command("curation_worker", once=True, stderr=io.StringIO())
                self.assertEqual(beat.call_count, 2)
                sleep.assert_called_once_with(2)
                self.assertFalse(WorkItem.objects.exists())
                self.assertFalse(WorkAttempt.objects.exists())

    def test_idle_worker_does_not_hide_non_lock_database_failures(self):
        from django.core.management import call_command
        from django.db import OperationalError

        module = "apps.curation.management.commands.curation_worker"
        with (
            patch(module + ".signal.signal"),
            patch(
                module + ".heartbeat", side_effect=OperationalError("disk I/O error")
            ),
            patch(module + ".claim") as claim,
            self.assertRaisesMessage(OperationalError, "disk I/O error"),
        ):
            call_command("curation_worker", once=True, stderr=io.StringIO())
        claim.assert_not_called()


def settings_policy():
    from django.conf import settings

    return settings.CURATION_POLICY_VERSION
