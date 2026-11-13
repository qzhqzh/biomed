import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.curation.classification import (
    classify,
    classify_metadata,
    taxonomy_hash,
    taxonomy_snapshot,
)
from apps.curation.collections import collection_data
from apps.curation.management.commands.literature_classify import reclassify_collection
from apps.curation.materials import pubmed_metadata, save_pubmed_article
from apps.curation.models import (
    CollectionEntry,
    LiteratureClassification,
    LiteratureCollection,
    WorkItem,
)
from apps.curation.network import SourceBlocked, xml
from apps.curation.test_collections import article, articles


class BoundaryRuleTests(SimpleTestCase):
    def result(self, title="", **metadata):
        return classify_metadata({"title": title, **metadata}, taxonomy_snapshot())

    def test_organ_mentions_do_not_turn_blood_cancers_into_solid_cancers(self):
        for title in (
            "Primary central nervous system lymphoma",
            "Cutaneous T-cell lymphoma of the skin",
            "Breast lymphoma: an observational study",
            "Plasma cell neoplasms and renal function",
            "Waldenström macroglobulinemia",
            "Myeloid sarcoma of the breast",
            "Histiocytic sarcoma of the skin",
            "CLL: current treatment",
            "Leukaemic cells in bone marrow",
        ):
            with self.subTest(title=title):
                self.assertEqual(self.result(title)["scope"], "hematologic_candidate")

    def test_mixed_blood_and_solid_cancers_are_retained(self):
        result = self.result("Treatment of lung carcinoma and lymphoma")
        self.assertEqual(result["scope"], "mixed_candidate")

    def test_gene_names_and_common_words_do_not_count_as_blood_cancers(self):
        for title in (
            "Anaplastic lymphoma kinase in lung carcinoma",
            "Leukemia inhibitory factor in breast cancer",
        ):
            with self.subTest(title=title):
                self.assertEqual(self.result(title)["scope"], "solid_candidate")
        self.assertEqual(
            self.result("ALL participants underwent assessment")["scope"], "uncertain"
        )

    def test_benign_and_precursor_findings_keep_separate_boundaries(self):
        for title in (
            "Breast fibroadenoma: a cohort study",
            "Benign thyroid nodules",
            "Non-malignant liver lesions",
            "Uterine leiomyomas",
            "Pituitary adenoma",
        ):
            with self.subTest(title=title):
                self.assertEqual(self.result(title)["scope"], "benign_candidate")
        for title in ("Colorectal polyps", "Cervical intraepithelial neoplasia"):
            with self.subTest(title=title):
                self.assertEqual(self.result(title)["scope"], "precursor_candidate")
        self.assertEqual(
            self.result("Normal breast tissue imaging")["scope"], "uncertain"
        )
        self.assertEqual(
            self.result("Breast carcinoma compared with benign lesions")["scope"],
            "solid_boundary_candidate",
        )

    def test_retraction_notice_and_retracted_paper_are_distinct(self):
        for publication_type in ("Retraction Notice", "Retraction of Publication"):
            with self.subTest(publication_type=publication_type):
                self.assertEqual(
                    self.result(publication_types=[publication_type])[
                        "publication_status"
                    ],
                    "retraction_notice",
                )
        self.assertEqual(
            self.result(publication_types=["Retracted Publication"])[
                "publication_status"
            ],
            "retracted_publication",
        )
        for title in (
            "Statement of Retraction: Long noncoding RNA in thyroid carcinoma",
            "Retraction: A study of breast cancer",
            "[Retraction] A study of breast cancer",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    self.result(title)["publication_status"], "retraction_notice"
                )

    def test_retraction_topic_and_surgical_retraction_are_not_article_statuses(self):
        result = self.result(
            "Retraction in cancer research: a systematic review",
            abstract="We studied retracted publications and retraction notices.",
            mesh=["Retraction Notice as Topic"],
            publication_types=["Review"],
        )
        self.assertEqual(result["publication_status"], "unmarked")
        self.assertEqual(
            self.result(
                "Retraction of publications in oncology: a bibliometric analysis"
            )["publication_status"],
            "unmarked",
        )
        self.assertEqual(
            self.result("Nipple retraction in breast cancer")["publication_status"],
            "unmarked",
        )

    def test_indexed_status_overrides_conflicting_title_and_preserves_the_conflict(
        self,
    ):
        metadata = {
            "title": "[Retracted] miR-29a inhibits human retinoblastoma progression",
            "publication_types": ["Journal Article", "Retraction Notice"],
        }
        result = classify_metadata(metadata, taxonomy_snapshot())
        self.assertEqual(result["publication_status"], "retraction_notice")
        self.assertEqual(result["publication_status_basis"], "publication_types")
        self.assertNotIn(
            "retracted_publication", {label["id"] for label in result["labels"]}
        )
        self.assertEqual(
            result["publication_status_conflicts"][0]["id"], "retracted_publication"
        )
        legacy = classify_metadata(metadata, taxonomy_snapshot("solid-tumor-rules-v2"))
        self.assertEqual(legacy["publication_status"], "retracted_publication")

    def test_corrections_and_concerns_are_not_retractions(self):
        self.assertEqual(
            self.result(
                "Correction: Breast cancer", publication_types=["Published Erratum"]
            )["publication_status"],
            "correction",
        )
        self.assertEqual(
            self.result("Expression of Concern: Breast cancer")["publication_status"],
            "expression_of_concern",
        )

    def test_match_offsets_and_unreviewed_status_are_preserved(self):
        result = self.result("Retraction: Lung carcinoma with lymphoma")
        self.assertEqual(result["review_status"], "unreviewed")
        for label in result["labels"]:
            for match in label["matches"]:
                self.assertEqual(
                    result["input"][match["field"]][match["start"] : match["end"]],
                    match["text"],
                )

    def test_legacy_taxonomy_retains_legacy_scope_logic(self):
        metadata = {
            "title": "Primary brain lymphoma",
            "publication_types": ["Retraction Notice"],
        }
        result = classify_metadata(metadata, taxonomy_snapshot("solid-tumor-rules-v1"))
        self.assertEqual(result["scope"], "mixed_candidate")
        self.assertNotIn("publication_status", result)
        self.assertEqual(
            self.result("Primary brain lymphoma")["scope"], "hematologic_candidate"
        )


class ContextRuleTests(SimpleTestCase):
    def result(self, **metadata):
        return classify_metadata(metadata, taxonomy_snapshot("solid-tumor-rules-v3"))

    def test_molecular_names_do_not_invent_a_disease_and_keep_offsets(self):
        cases = (
            (
                {
                    "title": "Bone development",
                    "abstract": "Neuroblastoma amplified sequence gene variants affect bone.",
                },
                "uncertain",
            ),
            (
                {
                    "title": "Lung cancer",
                    "mesh": ["Myeloid Cell Leukemia Sequence 1 Protein"],
                },
                "solid_candidate",
            ),
        )
        for metadata, scope in cases:
            with self.subTest(metadata=metadata):
                result = self.result(**metadata)
                self.assertEqual(result["scope"], scope)
                self.assertTrue(result["suppressed_matches"])
                for match in result["suppressed_matches"]:
                    self.assertEqual(
                        result["input"][match["field"]][match["start"] : match["end"]],
                        match["text"],
                    )
                legacy = classify_metadata(
                    metadata, taxonomy_snapshot("solid-tumor-rules-v2.1")
                )
                self.assertNotEqual(legacy["scope"], scope)

    def test_a_real_disease_after_an_excluded_name_is_still_found(self):
        result = self.result(
            title="MCL1 signaling in lung cancer",
            abstract="Myeloid cell leukemia 1 protein was measured. We also enrolled leukemia patients.",
        )
        self.assertEqual(result["scope"], "mixed_candidate")
        result = self.result(
            title="Neuroblastoma amplified sequence expression in neuroblastoma"
        )
        self.assertEqual(result["scope"], "solid_candidate")

    def test_normal_comparators_are_not_benign_lesions(self):
        for phrase in (
            "non-malignant SVG p12 cells",
            "nonmalignant immune populations",
            "non-cancerous background",
            "non-malignant counterparts",
        ):
            with self.subTest(phrase=phrase):
                result = self.result(
                    title="Glioblastoma response", abstract=f"We examined {phrase}."
                )
                self.assertEqual(result["scope"], "solid_candidate")
                self.assertNotIn("benign", {x["id"] for x in result["labels"]})
        self.assertEqual(
            self.result(title="Non-malignant cells in cancer initiation")["scope"],
            "uncertain",
        )

    def test_real_benign_comparisons_remain_after_normal_cell_exclusion(self):
        result = self.result(
            title="Breast carcinoma",
            abstract="We tested non-malignant cells and benign tumors in separate cohorts.",
        )
        self.assertEqual(result["scope"], "solid_boundary_candidate")
        self.assertEqual(
            self.result(title="Benign thyroid nodules")["scope"], "benign_candidate"
        )
        self.assertEqual(
            self.result(
                title="Gastric cancer",
                abstract="We distinguished benign and malignant conditions.",
            )["scope"],
            "solid_boundary_candidate",
        )

    def test_new_site_names_require_cancer_not_only_anatomy(self):
        for site in (
            "Oropharyngeal",
            "Gastrointestinal",
            "Gynecological",
            "Gynaecologic",
        ):
            with self.subTest(site=site):
                self.assertEqual(
                    self.result(title=f"{site} cancer outcomes")["scope"],
                    "solid_candidate",
                )
                self.assertEqual(
                    self.result(title=f"{site} imaging")["scope"], "uncertain"
                )

    def test_curated_cell_models_require_oncology_context(self):
        for model in ("HCT116", "HCT 116", "JIMT1", "JIMT-1"):
            with self.subTest(model=model):
                self.assertEqual(
                    self.result(
                        title="Tumor cell capture",
                        abstract=f"We studied {model} cells.",
                    )["scope"],
                    "solid_candidate",
                )
                self.assertEqual(
                    self.result(title=f"Instrument calibration using {model}")["scope"],
                    "uncertain",
                )
        self.assertEqual(
            self.result(title="Cell capture using an unknown cell line")["scope"],
            "uncertain",
        )


class ClassificationRevisionTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = override_settings(CURATION_STORAGE_DIR=Path(directory.name))
        context.enable()
        self.addCleanup(context.disable)
        self.old = taxonomy_snapshot("solid-tumor-rules-v1")
        self.new = taxonomy_snapshot()
        from apps.curation.collections import DEFAULT_LIMITS

        self.run = LiteratureCollection.objects.create(
            start_date="2021-10-15",
            end_date="2026-10-15",
            state="paused",
            query=self.old["query"],
            taxonomy=self.old,
            limits=DEFAULT_LIMITS,
        )
        self.versions = []
        self.metadata = []
        for pmid in ("123", "456"):
            node = xml(articles(article(pmid=pmid))).find("./PubmedArticle")
            _, version, metadata = save_pubmed_article(
                node, "https://example.org/source"
            )
            CollectionEntry.objects.create(
                collection=self.run,
                pmid=pmid,
                metadata_version=version,
                identifiers=metadata["identifiers"],
            )
            classify(version, metadata, self.old)
            self.versions.append(version)
            self.metadata.append(pubmed_metadata(node))

    def test_append_from_raw_metadata_is_idempotent_and_preserves_old_records(self):
        before = list(LiteratureClassification.objects.order_by("id").values())
        self.versions[0].material.metadata = {"title": "Changed mutable metadata"}
        self.versions[0].material.save()
        result = reclassify_collection(self.run.pk, self.new)
        self.assertEqual(result["created"], 2)
        again = reclassify_collection(self.run.pk, self.new)
        self.assertEqual(
            (again["created"], again["reused"], again["model_calls"]), (0, 2, 0)
        )
        self.assertEqual(
            list(
                LiteratureClassification.objects.filter(
                    taxonomy_sha256=taxonomy_hash(self.old)
                )
                .order_by("id")
                .values()
            ),
            before,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.taxonomy, self.old)
        new = LiteratureClassification.objects.filter(
            taxonomy_sha256=taxonomy_hash(self.new)
        ).first()
        self.assertEqual(
            new.result["input"]["title"], "Lung carcinoma randomized trial"
        )
        self.assertFalse(WorkItem.objects.exists())

    def test_only_complete_revisions_are_selected_and_old_view_is_available(self):
        classify(self.versions[0], self.metadata[0], self.new)
        self.assertEqual(
            collection_data(self.run)["classification_taxonomy_version"],
            self.old["version"],
        )
        with self.assertRaises(SourceBlocked):
            collection_data(self.run, classification_sha=taxonomy_hash(self.new))
        classify(self.versions[1], self.metadata[1], self.new)
        self.assertEqual(
            collection_data(self.run)["classification_taxonomy_version"],
            self.new["version"],
        )
        response = self.client.get(
            f"/api/v1/curation/collections/{self.run.pk}/",
            {"classification_sha": taxonomy_hash(self.old)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["classification_taxonomy_version"], self.old["version"]
        )
        self.assertEqual(len(response.json()["classification_revisions"]), 2)

    def test_running_or_queued_collection_rejects_reclassification(self):
        self.run.state = "running"
        self.run.save()
        with self.assertRaises(SourceBlocked):
            reclassify_collection(self.run.pk, self.new)
        self.run.state = "paused"
        self.run.save()
        WorkItem.objects.create(
            kind="collect_literature", payload={"collection_id": str(self.run.pk)}
        )
        with self.assertRaises(SourceBlocked):
            reclassify_collection(self.run.pk, self.new)
        self.assertEqual(LiteratureClassification.objects.count(), 2)

    def test_reused_new_revision_does_not_lose_priority_to_later_legacy_rows(self):
        reclassify_collection(self.run.pk, self.new)
        LiteratureClassification.objects.filter(
            taxonomy_sha256=taxonomy_hash(self.old)
        ).update(created_at=timezone.now() + timedelta(days=1))
        self.assertEqual(
            collection_data(self.run)["classification_taxonomy_version"],
            self.new["version"],
        )

    def test_source_identity_failure_leaves_old_classifications_intact(self):
        self.run.entries.filter(pmid="123").update(pmid="789")
        with self.assertRaises(SourceBlocked):
            reclassify_collection(self.run.pk, self.new)
        self.assertEqual(LiteratureClassification.objects.count(), 2)

    def test_reclassification_resumes_from_committed_chunks_after_interruption(self):
        for pmid in range(1000, 1050):
            node = xml(articles(article(pmid=str(pmid)))).find("./PubmedArticle")
            _, version, metadata = save_pubmed_article(
                node, "https://example.org/source"
            )
            CollectionEntry.objects.create(
                collection=self.run,
                pmid=str(pmid),
                metadata_version=version,
                identifiers=metadata["identifiers"],
            )
        calls = 0

        def interrupted(version, metadata, taxonomy):
            nonlocal calls
            calls += 1
            if calls == 51:
                raise SourceBlocked("simulated stop after first committed chunk")
            return classify(version, metadata, taxonomy)

        with (
            patch(
                "apps.curation.management.commands.literature_classify.classify",
                side_effect=interrupted,
            ),
            self.assertRaises(SourceBlocked),
        ):
            reclassify_collection(self.run.pk, self.new)
        self.assertEqual(
            LiteratureClassification.objects.filter(
                taxonomy_sha256=taxonomy_hash(self.new)
            ).count(),
            50,
        )
        result = reclassify_collection(self.run.pk, self.new)
        self.assertEqual((result["created"], result["reused"]), (2, 50))

    def test_taxonomy_endpoint_is_versioned_and_rejects_unknown_paths(self):
        response = self.client.get(
            "/api/v1/curation/literature-taxonomy/", {"version": self.old["version"]}
        )
        self.assertEqual(response.json(), self.old)
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/literature-taxonomy/", {"version": "../secret"}
            ).status_code,
            400,
        )
