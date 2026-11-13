import copy
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from apps.curation.decision import checked_input, digest, validate_decision


class DecisionValidationTests(SimpleTestCase):
    def setUp(self):
        self.metadata = {
            "title": "Lung cancer cohort",
            "abstract": "Patients with lung cancer and a history of benign disease.",
            "mesh": ["Lung Neoplasms"],
            "publication_types": ["Journal Article"],
        }
        self.output = {
            "relevance": "solid",
            "reason": "The cohort studies lung cancer.",
            "evidence": [{"field": "title", "quote": "Lung cancer"}],
            "labels": [
                {
                    "axis": "site",
                    "id": "lung",
                    "role": "study_subject",
                    "field": "abstract",
                    "quote": "lung cancer",
                }
            ],
        }

    def test_candidate_has_exact_source_offsets_and_is_not_reviewed(self):
        result = validate_decision(json.dumps(self.output), self.metadata)
        self.assertEqual(result["scope"], "solid_candidate")
        self.assertEqual(result["review_status"], "unreviewed")
        self.assertNotIn("publication_status", result)
        span = result["labels"][0]["matches"][0]
        self.assertEqual(
            self.metadata[span["field"]][span["start"] : span["end"]], "lung cancer"
        )

    def test_hallucinated_or_paraphrased_quote_is_rejected(self):
        for quote in ["advanced lung cancer", "Lung ... cohort", ""]:
            self.output["evidence"][0]["quote"] = quote
            with self.assertRaises(ValueError):
                validate_decision(json.dumps(self.output), self.metadata)

    def test_unknown_axis_label_role_and_duplicate_are_rejected(self):
        for field in ("axis", "id", "role"):
            output = copy.deepcopy(self.output)
            output["labels"][0][field] = "invented"
            with self.assertRaises(ValueError):
                validate_decision(json.dumps(output), self.metadata)
        self.output["labels"] *= 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_decision(json.dumps(self.output), self.metadata)

    def test_non_neoplastic_and_unspecified_are_distinct_under_compatible_scope(self):
        for relevance in ("non_neoplastic", "oncology_unspecified"):
            self.output["relevance"] = relevance
            result = validate_decision(json.dumps(self.output), self.metadata)
            self.assertEqual(result["scope"], "uncertain")
            self.assertEqual(result["relevance"], relevance)

    def test_no_source_evidence_only_allowed_for_insufficient(self):
        self.output["evidence"] = []
        with self.assertRaises(ValueError):
            validate_decision(json.dumps(self.output), self.metadata)
        self.output["relevance"] = "insufficient"
        self.output["labels"] = []
        self.assertEqual(
            validate_decision(json.dumps(self.output), self.metadata)["scope"],
            "uncertain",
        )

    def test_input_integrity_rejects_changed_source_and_strips_reference_labels(self):
        row = {
            "pmid": "123",
            "version_id": "source-version",
            "source_sha256": "a" * 64,
            "input_sha256": digest(self.metadata),
            "metadata": copy.deepcopy(self.metadata),
            "reference": {"scope": "solid_candidate"},
            "baseline": {"scope": "solid_candidate"},
        }
        self.assertNotIn("reference", checked_input(row))
        self.assertNotIn("baseline", checked_input(row))
        row["metadata"]["abstract"] += " changed"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            checked_input(row)

    def test_extra_status_and_multiple_json_objects_cannot_be_smuggled_in(self):
        with self.assertRaises(ValueError):
            validate_decision(
                json.dumps(self.output) + json.dumps(self.output), self.metadata
            )
        self.output["publication_status"] = "unmarked"
        with self.assertRaises(ValueError):
            validate_decision(json.dumps(self.output), self.metadata)


class DecisionImportTests(TestCase):
    def setUp(self):
        from apps.curation.classification import classify, taxonomy_snapshot
        from apps.curation.decision import VERSION
        from apps.curation.materials import save_pubmed_article
        from apps.curation.network import xml
        from apps.curation.test_collections import article, articles

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        settings = override_settings(CURATION_STORAGE_DIR=self.root / "blobs")
        settings.enable()
        self.addCleanup(settings.disable)
        raw = articles(article()).replace(
            b"Randomized Controlled Trial", b"Retraction Notice"
        )
        node = xml(raw).find("./PubmedArticle")
        _, self.version, metadata = save_pubmed_article(
            node, "https://example.org/source"
        )
        classify(self.version, metadata, taxonomy_snapshot("solid-tumor-rules-v1"))
        metadata = {
            key: metadata[key]
            for key in ("title", "abstract", "mesh", "publication_types")
        }
        self.row = {
            "pmid": "123",
            "version_id": str(self.version.pk),
            "source_sha256": self.version.sha256,
            "input_sha256": digest(metadata),
            "metadata": metadata,
        }
        self.config = {"version": VERSION, "model_id": "test-model"}
        output = {
            "relevance": "solid",
            "reason": "Source mentions lung carcinoma.",
            "evidence": [{"field": "title", "quote": "Lung carcinoma"}],
            "labels": [],
        }
        self.prediction = {
            **{
                k: self.row[k]
                for k in ("pmid", "version_id", "source_sha256", "input_sha256")
            },
            "config_sha256": digest(self.config),
            "created_at": "2026-10-16T00:00:00Z",
            "seconds": 1,
            "input_tokens": 100,
            "output_tokens": 30,
            "status": "validated_candidate",
            "raw_output": json.dumps(output),
            "decision": validate_decision(json.dumps(output), metadata),
        }

    def write_run(self):
        manifest = {
            "config": self.config,
            "config_sha256": digest(self.config),
            "input_sha256": digest([self.row]),
            "records": 1,
        }
        for filename, data in {
            "manifest.json": manifest,
            "inputs.json": {"records": [self.row]},
            "result-123.json": self.prediction,
        }.items():
            (self.root / filename).write_text(json.dumps(data))

    def test_import_is_idempotent_preserves_rules_and_source_retraction(self):
        from apps.curation.management.commands.literature_import_decisions import (
            import_decisions,
        )
        from apps.curation.models import Evidence, LiteratureClassification, WorkItem

        self.write_run()
        original = list(LiteratureClassification.objects.values())
        self.assertEqual(import_decisions(self.root)["created"], 1)
        self.assertEqual(import_decisions(self.root)["reused"], 1)
        self.assertEqual(
            list(
                LiteratureClassification.objects.filter(pk=original[0]["id"]).values()
            ),
            original,
        )
        result = LiteratureClassification.objects.latest("id").result
        self.assertEqual(result["publication_status"], "retraction_notice")
        self.assertEqual(result["review_status"], "unreviewed")
        self.assertFalse(Evidence.objects.exists())
        self.assertFalse(WorkItem.objects.exists())

    def test_modified_inputs_even_with_matching_bundle_hash_cannot_replace_source(self):
        from apps.curation.management.commands.literature_import_decisions import (
            import_decisions,
        )
        from apps.curation.models import LiteratureClassification

        self.row["metadata"]["abstract"] += " Tampered source."
        self.row["input_sha256"] = digest(self.row["metadata"])
        self.prediction["input_sha256"] = self.row["input_sha256"]
        self.write_run()
        with self.assertRaisesRegex(ValueError, "原始元数据"):
            import_decisions(self.root)
        self.assertEqual(LiteratureClassification.objects.count(), 1)

    def test_invalid_output_is_recorded_but_never_imported_as_a_label(self):
        from apps.curation.management.commands.literature_import_decisions import (
            import_decisions,
        )
        from apps.curation.models import LiteratureClassification

        self.prediction.update(
            status="invalid_output", raw_output="bad JSON", decision=None
        )
        self.write_run()
        result = import_decisions(self.root)
        self.assertEqual((result["created"], result["rejected_outputs"]), (0, 1))
        self.assertEqual(LiteratureClassification.objects.count(), 1)

    def test_complete_experimental_revision_does_not_replace_default_rules(self):
        from apps.curation.classification import taxonomy_snapshot
        from apps.curation.collections import DEFAULT_LIMITS, collection_data
        from apps.curation.management.commands.literature_import_decisions import (
            import_decisions,
        )
        from apps.curation.models import CollectionEntry, LiteratureCollection

        taxonomy = taxonomy_snapshot("solid-tumor-rules-v1")
        run = LiteratureCollection.objects.create(
            start_date="2021-10-15",
            end_date="2026-10-15",
            state="paused",
            query=taxonomy["query"],
            taxonomy=taxonomy,
            limits=DEFAULT_LIMITS,
        )
        CollectionEntry.objects.create(
            collection=run, pmid="123", metadata_version=self.version
        )
        self.write_run()
        import_decisions(self.root)
        status = collection_data(run)
        self.assertEqual(
            status["classification_taxonomy_version"], "solid-tumor-rules-v1"
        )
