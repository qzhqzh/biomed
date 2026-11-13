"""Keep background progress reads independent from large classification JSON."""

import tempfile
from pathlib import Path

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.curation.classification import taxonomy_hash, taxonomy_snapshot
from apps.curation.collections import DEFAULT_LIMITS, collection_data
from apps.curation.models import (
    CollectionEntry,
    LiteratureClassification,
    LiteratureCollection,
    Material,
    MaterialVersion,
)
from apps.curation.network import SourceBlocked


class ListReadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.taxonomy = taxonomy_snapshot()
        self.sha = taxonomy_hash(self.taxonomy)
        self.run = LiteratureCollection.objects.create(
            start_date="2021-10-15",
            end_date="2026-10-15",
            taxonomy=self.taxonomy,
            query="test",
            limits=DEFAULT_LIMITS,
            state="paused",
        )
        self.add_entry("1", "solid_candidate", "unmarked", reused=True)
        self.add_entry("2", "hematologic_candidate", "retraction_notice")
        CollectionEntry.objects.create(collection=self.run, pmid="3")

    def add_entry(self, pmid, scope, status, reused=False):
        material = Material.objects.create(
            source_key=f"pubmed:{pmid}",
            title=f"Paper {pmid}",
            identifiers={"pubmed": pmid},
            metadata={"abstract": "large text" * 100},
        )
        version = MaterialVersion.objects.create(
            material=material,
            sha256=pmid.zfill(64),
            media_type="application/pubmed+xml",
            filename="source.xml",
            rights="test",
        )
        CollectionEntry.objects.create(
            collection=self.run,
            pmid=pmid,
            metadata_version=version,
            reused=reused,
            fulltext_state="parsed" if reused else "pending",
        )
        LiteratureClassification.objects.create(
            version=version,
            taxonomy_version=self.taxonomy["version"],
            taxonomy_sha256=self.sha,
            result={"scope": scope, "publication_status": status},
        )
        return version

    def test_summary_does_not_read_classifications_or_document_payloads(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                f"/api/v1/curation/collections/{self.run.pk}/", {"summary": "1"}
            )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(
            (result["saved"], result["discovered"], result["reused"]), (2, 3, 1)
        )
        self.assertEqual(result["fulltexts"], {"parsed": 1, "pending": 2})
        self.assertFalse(result["classification_statistics_loaded"])
        self.assertIsNone(result["classification_taxonomy_sha256"])
        self.assertNotIn("entries", result)
        for query in queries:
            self.assertNotIn('"curation_literatureclassification"', query["sql"])
            self.assertNotIn('"curation_material"', query["sql"])

    def test_combined_statistics_preserve_scope_and_publication_counts(self):
        result = collection_data(self.run, detail=True)
        self.assertEqual(
            result["scopes"], {"solid_candidate": 1, "hematologic_candidate": 1}
        )
        self.assertEqual(
            result["publication_statuses"], {"unmarked": 1, "retraction_notice": 1}
        )
        self.assertEqual(result["classification_revisions"][0]["count"], 2)
        self.assertEqual(result["classification_statistics_saved"], 2)
        self.assertEqual(len(result["entries"]), 3)
        self.assertIsNone(result["entries"][-1]["classification"])

    def test_cached_statistics_keep_their_denominator_while_collection_grows(self):
        before = collection_data(self.run, cached=True)
        self.add_entry("4", "solid_candidate", "unmarked")
        with CaptureQueriesContext(connection) as queries:
            after = collection_data(self.run, cached=True)
        self.assertEqual(after["saved"], 3)
        self.assertEqual(after["classification_statistics_saved"], 2)
        self.assertEqual(after["statistics_as_of"], before["statistics_as_of"])
        self.assertEqual(after["classification_taxonomy_sha256"], self.sha)
        self.assertFalse(
            any('"curation_literatureclassification"' in q["sql"] for q in queries)
        )
        fresh = collection_data(self.run, classification_sha=self.sha, cached=True)
        self.assertEqual(fresh["classification_statistics_saved"], 3)
        self.assertEqual(fresh["scopes"]["solid_candidate"], 2)

    def test_explicit_incomplete_revision_remains_rejected(self):
        version = MaterialVersion.objects.first()
        LiteratureClassification.objects.create(
            version=version,
            taxonomy_version="experimental",
            taxonomy_sha256="a" * 64,
            result={"scope": "solid_candidate"},
        )
        with self.assertRaises(SourceBlocked):
            collection_data(self.run, classification_sha="a" * 64, cached=True)

    def test_material_summary_is_compact_and_does_not_repeat_counts_or_sort(self):
        params = {"summary": "1", "kind": "paper", "limit": 1}
        first = self.client.get("/api/v1/curation/materials/", params).json()
        self.assertEqual(first["total"], 2)
        self.assertNotIn("metadata", first["results"][0])
        with CaptureQueriesContext(connection) as queries:
            again = self.client.get("/api/v1/curation/materials/", params).json()
        self.assertEqual(again, first)
        self.assertEqual(len(queries), 2)
        self.assertIn('"source_key" >=', queries[0]["sql"])
        self.assertIn('"source_key" <', queries[0]["sql"])
        self.assertNotIn("COUNT(", queries[1]["sql"])
        self.assertNotIn('"curation_material"."metadata"', queries[1]["sql"])
        full = self.client.get("/api/v1/curation/materials/", {"kind": "paper"}).json()
        self.assertIn("abstract", full["results"][0]["metadata"])

    def test_material_counts_still_apply_search_before_kind_and_pagination(self):
        Material.objects.create(
            source_key="guide:1", kind="guideline", title="Paper guide"
        )
        result = self.client.get(
            "/api/v1/curation/materials/",
            {"summary": "1", "kind": "guideline", "q": "Paper", "limit": 1},
        ).json()
        self.assertEqual(
            result["counts"], {"paper": 2, "guideline": 1, "other": 0, "all": 3}
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["kind"], "guideline")

    def test_upload_invalidates_all_list_variants_without_clearing_collector_cache(
        self,
    ):
        params = {"summary": "1", "kind": "paper", "limit": 50}
        before = self.client.get("/api/v1/curation/materials/", params).json()
        empty_search = self.client.get(
            "/api/v1/curation/materials/", {**params, "q": "Uploaded"}
        ).json()
        self.assertEqual(empty_search["total"], 0)
        # Background collection must not invalidate the short-lived UI snapshot.
        Material.objects.create(source_key="pubmed:next", title="Collected paper")
        cached = self.client.get("/api/v1/curation/materials/", params).json()
        self.assertEqual(cached, before)
        with (
            tempfile.TemporaryDirectory() as folder,
            self.settings(CURATION_STORAGE_DIR=Path(folder)),
        ):
            response = self.client.post(
                "/api/v1/curation/materials/",
                {
                    "file": SimpleUploadedFile("paper.pdf", b"%PDF-1.4\nnew upload"),
                    "title": "Uploaded paper",
                    "kind": "paper",
                    "rights": "User-owned",
                    "rights_confirmed": "true",
                    "start_parse": "false",
                },
            )
        self.assertEqual(response.status_code, 201)
        # Old cache entries deliberately remain; a fresh request derives the new
        # generation from shared DB state, even in another web worker.
        after = self.client.get("/api/v1/curation/materials/", params).json()
        self.assertEqual(after["total"], 4)
        self.assertEqual(after["results"][0]["title"], "Uploaded paper")
        search = self.client.get(
            "/api/v1/curation/materials/", {**params, "q": "Uploaded"}
        ).json()
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["results"][0]["title"], "Uploaded paper")
