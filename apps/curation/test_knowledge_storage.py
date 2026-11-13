import copy

from django.test import TestCase

from apps.curation.knowledge_storage import import_candidates
from apps.curation.models import (
    Evidence,
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    Material,
    MaterialVersion,
    Passage,
    WorkItem,
)


class KnowledgeStorageTests(TestCase):
    def setUp(self):
        self.material = Material.objects.create(
            source_key="test:knowledge",
            kind="paper",
            title="Anchored knowledge test",
            identifiers={"pubmed": "123", "pmc": "PMC123", "doi": "10.example/test"},
        )
        self.version = MaterialVersion.objects.create(
            material=self.material,
            sha256="a" * 64,
            media_type="application/xml",
            filename="test.xml",
            rights="test fixture",
            source_url="https://example.org/article",
        )
        self.text = "ICI is immune checkpoint inhibition. H3 K27M-mutant was studied."
        self.passage = Passage.objects.create(
            version=self.version,
            parser_version="test-parser-v1",
            ordinal=1,
            locator="body/p:1",
            text=self.text,
        )
        variant_start = self.text.index("H3 K27M-mutant")
        self.payload = {
            "schema_version": "knowledge-candidates-v1",
            "source_run": "test-knowledge-v1",
            "provenance": {
                "method": "frozen-test",
                "source_path": "/data/private/input.json",
            },
            "records": [
                {
                    "group_id": "K01",
                    "version_id": str(self.version.pk),
                    "source_sha256": self.version.sha256,
                    "article": {"material_id": str(self.material.pk), "pmid": "123"},
                    "fact": {"treatment": "ICI", "finding": "fixture"},
                    "assessment": {"status": "not_reviewed"},
                    "provenance": {"unresolved_entities": [{"text": "unknown"}]},
                    "entities": [
                        {
                            "ref": "K01.e1",
                            "kind": "treatment",
                            "subtype": "category",
                            "label": "ICI",
                        },
                        {"ref": "K01.e2", "kind": "variant", "label": "H3 K27M-mutant"},
                    ],
                    "anchors": [
                        {
                            "entity_ref": "K01.e1",
                            "passage_id": str(self.passage.pk),
                            "start": 0,
                            "end": 3,
                            "quote": "ICI",
                            "purpose": "mention",
                            "association": "context_only",
                            "roles": [],
                        },
                        {
                            "entity_ref": "K01.e2",
                            "passage_id": str(self.passage.pk),
                            "start": variant_start,
                            "end": variant_start + len("H3 K27M-mutant"),
                            "quote": "H3 K27M-mutant",
                            "purpose": "mention",
                            "association": "model_selected",
                            "roles": ["biomarker_candidate"],
                        },
                        {
                            "entity_ref": None,
                            "passage_id": str(self.passage.pk),
                            "start": 0,
                            "end": len(self.text),
                            "quote": self.text,
                            "purpose": "evidence",
                            "association": "context_only",
                            "roles": [],
                        },
                    ],
                }
            ],
        }

    def test_persistence_preserves_types_roles_and_two_level_provenance(self):
        result = import_candidates(self.payload)
        self.assertFalse(result["reused"])
        self.assertEqual(result["candidate_count"], 1)
        candidate = KnowledgeCandidate.objects.get()
        self.assertEqual(candidate.status, "research_candidate")
        self.assertEqual(candidate.version.material, self.material)
        variant = KnowledgeEntity.objects.get(kind="variant")
        anchor = variant.anchors.get()
        self.assertEqual(anchor.roles, ["biomarker_candidate"])
        self.assertEqual(anchor.quote, self.passage.text[anchor.start : anchor.end])
        self.assertEqual(anchor.passage.version, candidate.version)
        self.assertEqual(Evidence.objects.count(), 0)
        self.assertEqual(WorkItem.objects.count(), 0)

    def test_candidate_pagination_keeps_distinct_entity_and_anchor_counts(self):
        import_candidates(self.payload)
        candidate = KnowledgeCandidate.objects.get()
        original = candidate.anchors.get(entity__kind="treatment")
        KnowledgeAnchor.objects.create(
            candidate=candidate, entity=original.entity, passage=self.passage,
            content_sha256="f" * 64, entity_ref=original.entity_ref,
            start=original.start, end=original.end, quote=original.quote,
            purpose="mention", association="context_only", roles=[],
        )
        KnowledgeCandidate.objects.create(
            source_run=candidate.source_run, group_id="K02", version=self.version,
            content_sha256="b" * 64, import_sha256="c" * 64,
        )
        url = "/api/v1/curation/knowledge-candidates/"
        page = self.client.get(url, {"limit": 1}).json()
        self.assertEqual(page["count"], 2)
        self.assertEqual(page["results"][0]["group_id"], "K01")
        self.assertEqual(page["results"][0]["entity_count"], 2)
        self.assertEqual(page["results"][0]["anchor_count"], 4)
        second = self.client.get(url, {"limit": 1, "offset": 1}).json()
        self.assertEqual(second["results"][0]["entity_count"], 0)
        self.assertEqual(second["results"][0]["anchor_count"], 0)
        filtered = self.client.get(url, {"kind": "treatment"}).json()
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["results"][0]["anchor_count"], 4)
        detail = self.client.get(f"{url}{candidate.pk}/").json()
        self.assertEqual((detail["entity_count"], detail["anchor_count"]), (2, 4))
        entity = self.client.get(f"/api/v1/curation/knowledge-entities/{original.entity_id}/").json()
        self.assertEqual(entity["candidate_count"], 1)
        self.assertEqual(entity["candidates"][0]["anchor_count"], 4)

    def test_dry_run_and_idempotency_do_not_duplicate_or_publish(self):
        dry = import_candidates(self.payload, dry_run=True)
        self.assertEqual(dry["anchor_count"], 3)
        self.assertFalse(KnowledgeCandidate.objects.exists())
        first = import_candidates(self.payload)
        second = import_candidates(copy.deepcopy(self.payload))
        self.assertTrue(second["reused"])
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(KnowledgeAnchor.objects.count(), 3)
        self.assertEqual(KnowledgeEntity.objects.count(), 2)

    def test_import_hash_includes_run_provenance_and_no_overwrite(self):
        import_candidates(self.payload)
        modified = copy.deepcopy(self.payload)
        modified["provenance"]["method"] = "changed"
        with self.assertRaisesRegex(ValueError, "different content"):
            import_candidates(modified)
        self.assertEqual(KnowledgeCandidate.objects.count(), 1)

    def test_new_run_cannot_overwrite_scoped_entity_attributes(self):
        import_candidates(self.payload)
        modified = copy.deepcopy(self.payload)
        modified["source_run"] = "test-v2"
        modified["records"][0]["entities"][0]["attributes"] = {"definition": "new"}
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            import_candidates(modified)
        self.assertEqual(KnowledgeCandidate.objects.count(), 1)

    def test_bad_quote_rejects_whole_batch_before_write(self):
        modified = copy.deepcopy(self.payload)
        second = copy.deepcopy(modified["records"][0])
        second["group_id"] = "K02"
        second["anchors"][0]["quote"] = "ici"
        modified["records"].append(second)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            import_candidates(modified)
        self.assertEqual(KnowledgeCandidate.objects.count(), 0)
        self.assertEqual(KnowledgeEntity.objects.count(), 0)

    def test_cross_version_passage_is_rejected(self):
        other = MaterialVersion.objects.create(
            material=self.material,
            sha256="b" * 64,
            media_type="application/xml",
            filename="v2.xml",
            rights="test",
        )
        passage = Passage.objects.create(
            version=other,
            parser_version="test-parser-v1",
            ordinal=1,
            locator="body/p:1",
            text=self.text,
        )
        self.payload["records"][0]["anchors"][0]["passage_id"] = str(passage.pk)
        with self.assertRaisesRegex(ValueError, "another version"):
            import_candidates(self.payload)
        self.assertFalse(KnowledgeCandidate.objects.exists())

    def test_quarantined_and_wrong_article_or_hash_are_rejected(self):
        for field, value in (("source_sha256", "c" * 64), ("article", {"pmid": "456"})):
            modified = copy.deepcopy(self.payload)
            modified["records"][0][field] = value
            with self.assertRaises(ValueError):
                import_candidates(modified)
        self.version.integrity_status = "quarantined"
        self.version.save(update_fields=["integrity_status"])
        with self.assertRaisesRegex(ValueError, "not valid"):
            import_candidates(self.payload)

    def test_legacy_pmid_key_remains_supported(self):
        self.material.identifiers["pmid"] = self.material.identifiers.pop("pubmed")
        self.material.save(update_fields=["identifiers"])
        result = import_candidates(self.payload)
        candidate_url = "/api/v1/curation/knowledge-candidates/"
        data = self.client.get(candidate_url, {"q": "123"}).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["article"]["pmid"], "123")
        self.assertEqual(data["results"][0]["id"], result["candidates"][0]["id"])

    def test_conflicting_pubmed_and_pmid_are_rejected(self):
        self.material.identifiers["pmid"] = "456"
        self.material.save(update_fields=["identifiers"])
        with self.assertRaisesRegex(ValueError, "identifiers conflict"):
            import_candidates(self.payload)
        self.assertFalse(KnowledgeCandidate.objects.exists())
        self.material.identifiers["pmid"] = "123"
        self.material.save(update_fields=["identifiers"])
        self.assertEqual(import_candidates(self.payload)["candidate_count"], 1)

    def test_unknown_entity_and_missing_anchor_are_rejected(self):
        modified = copy.deepcopy(self.payload)
        modified["records"][0]["anchors"][0]["entity_ref"] = "does-not-exist"
        with self.assertRaisesRegex(ValueError, "unknown"):
            import_candidates(modified)
        modified = copy.deepcopy(self.payload)
        modified["records"][0]["anchors"].pop(0)
        with self.assertRaisesRegex(ValueError, "no source anchor"):
            import_candidates(modified)

    def test_parser_version_and_locator_are_checked(self):
        for field in ("parser_version", "locator"):
            modified = copy.deepcopy(self.payload)
            modified["records"][0]["anchors"][0]["attributes"] = {field: "wrong"}
            with self.assertRaises(ValueError):
                import_candidates(modified)
        self.assertFalse(KnowledgeCandidate.objects.exists())

    def test_api_filters_pagination_and_real_source_details(self):
        result = import_candidates(self.payload)
        candidate_id = result["candidates"][0]["id"]
        entity_url = "/api/v1/curation/knowledge-entities/"
        candidate_url = "/api/v1/curation/knowledge-candidates/"
        data = self.client.get(
            entity_url, {"kind": "treatment", "q": "ICI", "limit": 1}
        ).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["candidate_count"], 1)
        entity_id = data["results"][0]["id"]
        detail = self.client.get(entity_url + entity_id + "/").json()
        self.assertEqual(detail["candidates"][0]["id"], candidate_id)
        self.assertEqual(detail["anchors"][0]["passage"]["text"], self.text)
        self.assertEqual(detail["article"]["pmid"], "123")
        self.assertFalse(detail["anchors_truncated"])
        data = self.client.get(
            candidate_url, {"source_run": "test-knowledge-v1", "group_id": "K01"}
        ).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["entity_count"], 2)
        self.assertEqual(
            self.client.get(candidate_url, {"q": "123"}).json()["count"], 1
        )
        detail = self.client.get(candidate_url + candidate_id + "/").json()
        self.assertEqual(detail["fact"], self.payload["records"][0]["fact"])
        self.assertNotIn("source_path", detail["provenance"]["run"])
        self.assertEqual(len(detail["anchors"]), 3)
        self.assertEqual(self.client.get(entity_url, {"limit": 101}).status_code, 400)
        self.assertEqual(
            self.client.get(candidate_url, {"offset": -1}).status_code, 400
        )
        self.assertEqual(self.client.post(candidate_url, {}).status_code, 405)

    def test_same_label_in_another_article_is_not_merged(self):
        import_candidates(self.payload)
        other = Material.objects.create(source_key="test:other", title="Other")
        version = MaterialVersion.objects.create(
            material=other,
            sha256="b" * 64,
            media_type="text/plain",
            filename="other",
            rights="test",
        )
        passage = Passage.objects.create(
            version=version,
            parser_version="test",
            ordinal=1,
            locator="body/p:1",
            text=self.text,
        )
        modified = copy.deepcopy(self.payload)
        modified["source_run"] = "test-other"
        record = modified["records"][0]
        record.update(
            version_id=str(version.pk), source_sha256=version.sha256, article={}
        )
        for anchor in record["anchors"]:
            anchor["passage_id"] = str(passage.pk)
        import_candidates(modified)
        self.assertEqual(KnowledgeEntity.objects.filter(label="ICI").count(), 2)
