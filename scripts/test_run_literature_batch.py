import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.curation import test_knowledge_target_mapping as mapping_fixtures
from apps.curation.decision import digest
from apps.curation.knowledge_export import digest as bundle_digest
from scripts.prepare_knowledge_intake import freeze, read, sha
from scripts.run_literature_batch import (
    CLASSIFIER,
    MODEL_PYTHON,
    VERSION,
    deliver_batch,
    delivery_classification,
    main,
    prepare_classification,
    resume_fulltext_fallback,
    route_with_fulltext,
    selected_documents,
)


class BatchInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        metadata = {"title": "Lung cancer", "abstract": "Treatment study.",
                    "mesh": [], "publication_types": []}
        self.document = {"pmid": "123", "model_input": {
            "pmid": "123", "version_id": "source-version", "source_sha256": "a" * 64,
            "input_sha256": digest(metadata), "metadata": metadata}}
        self.source = self.write_snapshot("snapshot.json", [self.document])

    def write_snapshot(self, name, documents):
        value = {"documents": documents}
        path = self.root / name
        freeze(path, {"snapshot": value, "sha256": digest(value)})
        return path

    def test_missing_metadata_is_accounted_without_dropping_other_articles(self):
        count = prepare_classification(self.root / "batch", [self.source], ["123", "456"])
        self.assertEqual(count, 1)
        status = json.loads((self.root / "batch/classification/source-status.json").read_text())
        self.assertEqual(status["unavailable"], [{"pmid": "456", "status": "metadata_unavailable"}])
        self.assertEqual(status["requested_pmids"], ["123", "456"])

    def test_conflicting_source_versions_are_not_silently_chosen(self):
        other = self.write_snapshot("other.json", [{**self.document, "title": "Different version"}])
        with self.assertRaisesRegex(ValueError, "Multiple frozen source versions"):
            selected_documents([self.source, other], ["123"])

    def test_tampered_snapshot_rejected(self):
        value = json.loads(self.source.read_text())
        value["snapshot"]["documents"][0]["pmid"] = "456"
        self.source.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            selected_documents([self.source], ["123"])

    def test_fulltext_routing_preserves_original_and_uses_effective_categories(self):
        batch = self.root / "batch"
        selection = {"records": [{"pmid": "123", "status": "classified", "category": "solid"},
                                 {"pmid": "456", "status": "classified", "category": "non_neoplastic"},
                                 {"pmid": "789", "status": "classified", "category": "non_neoplastic"}],
                     "solid_pmids": ["123"], "categories": {"solid": 1, "non_neoplastic": 2}}
        freeze(batch / "classification/selection.json", selection)
        original_bytes = (batch / "classification/selection.json").read_bytes()
        fallback = {"records": [{"pmid": "123", "original_status": "classified", "original_category": "solid",
                                "fallback_status": "already_solid"},
                               {"pmid": "456", "original_status": "classified", "original_category": "non_neoplastic",
                                "fallback_status": "classified", "fallback_category": "solid"},
                               {"pmid": "789", "original_status": "classified", "original_category": "non_neoplastic",
                                "fallback_status": "input_too_long", "fallback_category": None}],
                    "continue_pmids": ["456"]}

        def fake_run(folder, *, model_python):
            freeze(folder / "routing.json", fallback)
            return fallback

        with (patch("scripts.classify_fulltext_fallback.prepare"),
              patch("scripts.classify_fulltext_fallback.run", side_effect=fake_run) as run_fallback,
              patch("scripts.run_literature_batch.cached_fulltext_fallback", return_value=fallback)):
            routed, _ = route_with_fulltext(batch, [self.source], selection, model_python="unused")
            self.assertEqual(route_with_fulltext(batch, [self.source], selection, model_python="unused")[0], routed)
            run_fallback.assert_called_once()
        self.assertEqual((batch / "classification/selection.json").read_bytes(), original_bytes)
        self.assertEqual(routed["solid_pmids"], ["123", "456"])
        rows = {row["pmid"]: row for row in routed["records"]}
        self.assertEqual((rows["456"]["category"], rows["456"]["status"]), ("non_neoplastic", "classified"))
        self.assertEqual((rows["456"]["effective_category"], rows["456"]["effective_status"]), ("solid", "classified"))
        self.assertEqual(rows["456"]["classification_scope"], "fulltext")
        self.assertEqual((rows["789"]["effective_category"], rows["789"]["effective_status"]), (None, "unresolved"))
        self.assertEqual(routed["effective_categories"], {"solid": 2, "unresolved": 1})
        base = json.loads((batch / "classification-routing.json").read_text())
        self.assertEqual(base["records"], selection["records"])
        freeze(batch / "selection.json", {"requested_pmids": ["123", "456"], "selected_pmids": ["123", "456"]})
        self.assertEqual(delivery_classification(batch), routed)

    def test_old_frozen_batch_does_not_claim_separate_fulltext_followup(self):
        batch = self.root / "batch"
        selection = {"records": [{"pmid": "123", "status": "classified", "category": "solid"},
                                 {"pmid": "456", "status": "classified", "category": "non_neoplastic"}],
                     "solid_pmids": ["123"]}
        freeze(batch / "classification/selection.json", selection)
        freeze(batch / "selection.json", {"requested_pmids": ["123"], "selected_pmids": ["123"]})
        freeze(batch / "classification-routing.json", {**selection, "solid_pmids": ["123", "456"]})
        self.assertEqual(delivery_classification(batch), selection)

    def test_recovered_article_requires_matching_frozen_routing(self):
        batch = self.root / "batch"
        selection = {"records": [{"pmid": "123", "status": "classified", "category": "solid"},
                                 {"pmid": "456", "status": "classified", "category": "non_neoplastic"}],
                     "solid_pmids": ["123"]}
        freeze(batch / "classification/selection.json", selection)
        freeze(batch / "selection.json", {"requested_pmids": ["123", "456"], "selected_pmids": ["123", "456"]})
        with self.assertRaisesRegex(ValueError, "require matching full-text routing provenance"):
            delivery_classification(batch)

    def test_completed_fallback_reuses_checked_outputs_without_gpu(self):
        batch, folder = self.root / "batch", self.root / "batch/fallback-fulltext-v1"
        selection = {"records": [{"pmid": "123", "status": "classified", "category": "solid"}], "solid_pmids": ["123"]}
        selection_path = batch / "classification/selection.json"
        freeze(selection_path, selection)
        inputs, definition = {"records": []}, {"version": "fixture"}
        freeze(folder / "inputs.json", inputs)
        freeze(folder / "definition.json", definition)
        freeze(folder / "preparation.json", {"original_classification_file_sha256": sha(selection_path),
                                             "original_classification_sha256": digest(selection),
                                             "inputs_sha256": digest(inputs), "definition_sha256": digest(definition),
                                             "sources": {str(self.source.resolve()): {"file_sha256": sha(self.source)}}})
        fallback = {"preparation_sha256": sha(folder / "preparation.json"), "model_manifest_sha256": None,
                    "records": [{"pmid": "123", "original_status": "classified", "original_category": "solid",
                                 "fallback_status": "already_solid"}], "continue_pmids": []}
        freeze(folder / "routing.json", fallback)
        with (patch("scripts.classify_fulltext_fallback.prepare") as prepare_fallback,
              patch("scripts.classify_fulltext_fallback.run") as run_fallback):
            routed, _ = route_with_fulltext(batch, [self.source], selection, model_python="unused")
        prepare_fallback.assert_not_called()
        run_fallback.assert_not_called()
        self.assertEqual(routed["solid_pmids"], ["123"])
        (folder / "inputs.json").write_text('{"records":[{"id":"tampered"}]}')
        with self.assertRaisesRegex(ValueError, "inputs or provenance changed"):
            route_with_fulltext(batch, [self.source], selection, model_python="unused")

    def test_resuming_frozen_batch_does_not_reselect_or_refreeze_sources(self):
        batch = self.root / "batch"
        freeze(batch / "selection.json", {"requested_pmids": ["123", "456"], "selected_pmids": ["123", "456"]})
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123", "456"]})
        manifest_path = batch / "batch-manifest.json"
        freeze(manifest_path, {"version": VERSION, "pmids": ["123", "456"],
                               "snapshots": {str(self.source.resolve()): sha(self.source)},
                               "selection_sha256": sha(selection_file), "classifier": str(CLASSIFIER),
                               "model_python": str(MODEL_PYTHON), "target_written": False})
        original_manifest = manifest_path.read_bytes()
        metadata_only = {"records": [{"pmid": "123", "status": "classified", "category": "solid"},
                                     {"pmid": "456", "status": "classified", "category": "non_neoplastic"}],
                         "solid_pmids": ["123"], "categories": {"solid": 1, "non_neoplastic": 1}}
        argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                "--selection-file", str(selection_file)]
        with (patch.object(sys, "argv", argv),
              patch("scripts.run_literature_batch.prepare_classification"),
              patch("scripts.run_literature_batch.run_classification", return_value=metadata_only),
              patch("scripts.run_literature_batch.route_with_fulltext") as route,
              patch("scripts.run_literature_batch.freeze_snapshot_sources") as refreeze,
              patch("scripts.run_minimal_pipeline.screen_pipeline") as screen,
              patch("scripts.run_literature_batch.run_pipeline", return_value={"state": "batch_completed"}) as pipeline):
            main()
        route.assert_not_called()
        refreeze.assert_not_called()
        screen.assert_called_once()
        pipeline.assert_called_once()
        self.assertIsNone(pipeline.call_args.kwargs["per_document_limit"])
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual(json.loads((batch / "selection.json").read_text())["requested_pmids"], ["123", "456"])

    def test_limit_is_forwarded_and_bound_to_the_batch_manifest(self):
        batch = self.root / "batch"
        freeze(batch / "selection.json", {"requested_pmids": ["123"], "selected_pmids": ["123"]})
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123"]})
        metadata_only = {"records": [{"pmid": "123", "status": "classified", "category": "solid"}],
                         "solid_pmids": ["123"], "categories": {"solid": 1}}
        argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                "--selection-file", str(selection_file), "--per-document-limit", "3"]
        with (patch.object(sys, "argv", argv),
              patch("scripts.run_literature_batch.prepare_classification"),
              patch("scripts.run_literature_batch.run_classification", return_value=metadata_only),
              patch("scripts.run_minimal_pipeline.screen_pipeline"),
              patch("scripts.run_literature_batch.run_pipeline", return_value={"state": "batch_completed"}) as pipeline):
            main()
            main()
            self.assertEqual(pipeline.call_args.kwargs["per_document_limit"], 3)
            self.assertEqual(json.loads((batch / "batch-manifest.json").read_text())["per_document_limit"], 3)
            argv[-1] = "4"
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                main()
            self.assertEqual(pipeline.call_count, 2)

    def test_clinical_profile_is_frozen_and_inherited_on_resume(self):
        batch = self.root / "clinical"
        freeze(batch / "selection.json", {"requested_pmids": ["123"], "selected_pmids": ["123"]})
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123"]})
        argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                "--selection-file", str(selection_file), "--clinical-profile", "target-clinical-focus-v1"]
        with (patch.object(sys, "argv", argv),
              patch("scripts.run_literature_batch.prepare_classification"),
              patch("scripts.run_literature_batch.run_classification", return_value={"categories": {"solid": 1}}),
              patch("scripts.run_minimal_pipeline.screen_pipeline") as screening,
              patch("scripts.run_literature_batch.run_pipeline", return_value={"state": "batch_completed"}) as pipeline):
            main()
            del argv[-2:]
            main()
            self.assertEqual(pipeline.call_args.kwargs["relation_profile"], "target-clinical-focus-v1")
            self.assertEqual(screening.call_args.kwargs["relation_profile"], "target-clinical-focus-v1")
            self.assertEqual(read(batch / "batch-manifest.json")["clinical_profile"], "target-clinical-focus-v1")

    def test_nonpositive_limit_is_rejected_before_a_batch_is_created(self):
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123"]})
        batch = self.root / "batch"
        for value in ("0", "-1", "1.5"):
            argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                    "--selection-file", str(selection_file), "--per-document-limit", value]
            with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                main()
            self.assertFalse(batch.exists())

    def test_followup_limit_is_bound_to_child_and_forwarded_without_mutating_parent(self):
        batch = self.root / "batch"
        freeze(batch / "classification/selection.json", {"records": [], "solid_pmids": []})
        freeze(batch / "fallback-fulltext-v1/routing.json", {"source": "fixture"})
        fallback = {"records": [{"pmid": "123", "fallback_status": "classified", "fallback_category": "solid",
                                 "original_category": "non_neoplastic"}], "continue_pmids": ["123"]}
        with (patch("scripts.run_literature_batch.route_with_fulltext", return_value=({}, fallback)),
              patch("scripts.run_literature_batch.freeze_snapshot_sources"),
              patch("scripts.run_minimal_pipeline.screen_pipeline") as screen,
              patch("scripts.run_literature_batch.run_pipeline", return_value={"state": "batch_completed"}) as pipeline):
            resume_fulltext_fallback(batch, [self.source], "python", None, None, per_document_limit=3)
            resume_fulltext_fallback(batch, [self.source], "python", None, None, per_document_limit=3)
            child = batch / "batch-fulltext-followup"
            self.assertEqual(json.loads((child / "batch-manifest.json").read_text())["per_document_limit"], 3)
            self.assertEqual(pipeline.call_args.kwargs["per_document_limit"], 3)
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                resume_fulltext_fallback(batch, [self.source], "python", None, None, per_document_limit=4)
            self.assertEqual(screen.call_count, 2)
            self.assertEqual(pipeline.call_count, 2)

    def test_unlimited_followup_reuses_legacy_child_manifest_byte_for_byte(self):
        batch = self.root / "batch"
        freeze(batch / "classification/selection.json", {"records": [], "solid_pmids": []})
        fallback_path = batch / "fallback-fulltext-v1/routing.json"
        freeze(fallback_path, {"source": "fixture"})
        child_manifest = batch / "batch-fulltext-followup/batch-manifest.json"
        freeze(child_manifest, {"version": VERSION, "pmids": ["123"], "parent_source_run": "batch",
                                "fallback_routing_sha256": sha(fallback_path)})
        original = child_manifest.read_bytes()
        fallback = {"records": [{"pmid": "123", "fallback_status": "classified", "fallback_category": "solid",
                                 "original_category": "non_neoplastic"}], "continue_pmids": ["123"]}
        with (patch("scripts.run_literature_batch.route_with_fulltext", return_value=({}, fallback)),
              patch("scripts.run_literature_batch.freeze_snapshot_sources"),
              patch("scripts.run_minimal_pipeline.screen_pipeline"),
              patch("scripts.run_literature_batch.run_pipeline", return_value={"state": "batch_completed"}) as pipeline):
            resume_fulltext_fallback(batch, [self.source], "python", None, None)
        self.assertEqual(child_manifest.read_bytes(), original)
        self.assertIsNone(pipeline.call_args.kwargs["per_document_limit"])

    def test_followup_cli_preserves_legacy_parent_manifest_with_child_limit(self):
        batch = self.root / "batch"
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123"]})
        manifest_path = batch / "batch-manifest.json"
        freeze(manifest_path, {"version": VERSION, "pmids": ["123"],
                               "snapshots": {str(self.source.resolve()): sha(self.source)},
                               "selection_sha256": sha(selection_file), "classifier": str(CLASSIFIER),
                               "model_python": str(MODEL_PYTHON), "target_written": False})
        original = manifest_path.read_bytes()
        argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                "--selection-file", str(selection_file), "--resume-fulltext-fallback", "--per-document-limit", "3"]
        with (patch.object(sys, "argv", argv),
              patch("scripts.run_literature_batch.resume_fulltext_fallback", return_value={"state": "completed"}) as followup):
            main()
        self.assertEqual(manifest_path.read_bytes(), original)
        self.assertEqual(followup.call_args.kwargs["per_document_limit"], 3)

    def test_delivery_reuses_frozen_limit_without_requiring_cli_repetition(self):
        batch = self.root / "batch"
        selection_file = self.root / "pmids.json"
        freeze(selection_file, {"pmids": ["123"]})
        manifest_path = batch / "batch-manifest.json"
        freeze(manifest_path, {"version": VERSION, "pmids": ["123"],
                               "snapshots": {str(self.source.resolve()): sha(self.source)},
                               "selection_sha256": sha(selection_file), "classifier": str(CLASSIFIER),
                               "model_python": str(MODEL_PYTHON), "target_written": False,
                               "per_document_limit": 3})
        original = manifest_path.read_bytes()
        argv = ["run_literature_batch", "--root", str(batch), "--snapshot", str(self.source),
                "--selection-file", str(selection_file), "--deliver-only", "--dictionary", str(self.root / "dictionary.json")]
        with (patch.object(sys, "argv", argv),
              patch("scripts.run_literature_batch.deliver_batch", return_value={"state": "completed"}) as deliver):
            main()
            argv.extend(["--per-document-limit", "4"])
            with self.assertRaisesRegex(ValueError, "cannot change the frozen"):
                main()
        deliver.assert_called_once()
        self.assertEqual(manifest_path.read_bytes(), original)

    def test_followup_no_recovered_articles_still_has_frozen_result(self):
        batch = self.root / "batch"
        freeze(batch / "classification/selection.json", {"records": [], "solid_pmids": []})
        fallback = {"state": "completed", "records": [], "continue_pmids": []}
        with patch("scripts.run_literature_batch.route_with_fulltext", return_value=({}, fallback)):
            result = resume_fulltext_fallback(batch, [self.source], "python", None, None)
        self.assertEqual(result, {"state": "completed", "recovered_articles": 0})
        self.assertEqual(json.loads((batch / "fulltext-followup-result.json").read_text()), result)

    def test_final_compatibility_and_report_bind_to_the_delivered_knowledge(self):
        fixture = mapping_fixtures.KnowledgeTargetMappingTests()
        fixture.setUp()
        batch = self.root / "batch"
        freeze(batch / "delivery/bundle.json", fixture.bundle)
        freeze(batch / "delivery/summary.json", {
            "state": "batch_completed", "article_outcomes": [{"pmid": "synthetic", "status": "processed"}]})
        classification = {"records": [{"pmid": "synthetic", "category": "non_neoplastic", "status": "classified"}],
                          "solid_pmids": []}
        freeze(batch / "classification/selection.json", classification)
        freeze(batch / "selection.json", {"requested_pmids": ["synthetic"], "selected_pmids": ["synthetic"]})
        fallback = {"records": [{"pmid": "synthetic", "original_category": "non_neoplastic", "original_status": "classified",
                                 "fallback_status": "classified", "fallback_category": "solid"}],
                    "continue_pmids": ["synthetic"]}

        def fake_run(folder, *, model_python):
            freeze(folder / "routing.json", fallback)
            return fallback

        with (patch("scripts.classify_fulltext_fallback.prepare"),
              patch("scripts.classify_fulltext_fallback.run", side_effect=fake_run)):
            route_with_fulltext(batch, [self.source], classification, model_python="unused")
        dictionary = self.root / "dictionary.json"
        freeze(dictionary, fixture.dictionary)
        with patch("apps.curation.knowledge_target_mapping.load_gene_standard", return_value=fixture.gene_standard):
            result = deliver_batch(batch, dictionary)
            self.assertEqual(deliver_batch(batch, dictionary), result)
        final = json.loads((batch / "final/knowledge.json").read_text())
        report = json.loads((batch / "final/target-mapping-report.json").read_text())
        exported = json.loads((batch / "final/tumor-api-export.json").read_text())
        self.assertEqual(report["source_bundle_sha256"], bundle_digest(final))
        self.assertEqual(exported["source_bundle_sha256"], bundle_digest(final))
        self.assertEqual(final["records"][0]["fact"], fixture.record["fact"])
        self.assertEqual(final["records"][0]["anchors"], fixture.record["anchors"])
        self.assertEqual(result["offline_ready_count"], 1)
        self.assertEqual(result["articles"][0]["category"], "non_neoplastic")
        self.assertEqual(result["articles"][0]["effective_category"], "solid")
        self.assertEqual(result["articles"][0]["extraction"]["status"], "processed")
        self.assertFalse(result["submit_ready"])
        self.assertFalse((batch / "final/import-receipt.json").exists())

    def test_automatic_normalization_uses_current_local_registry_and_saves_rules_before_knowledge(self):
        fixture = mapping_fixtures.KnowledgeTargetMappingTests()
        fixture.setUp()
        batch = self.root / "new-batch"
        freeze(batch / "delivery/bundle.json", fixture.bundle)
        freeze(batch / "delivery/summary.json", {"state": "batch_completed", "article_outcomes": []})
        freeze(batch / "classification/selection.json", {"records": [], "solid_pmids": []})
        dictionary = self.root / "dictionary.json"
        freeze(dictionary, fixture.dictionary)
        normalized = self.root / "normalized-registry.json"
        registry = fixture.registry()
        freeze(normalized, registry)
        patch_path = self.root / "registry-patch.json"
        freeze(patch_path, {"fixture": True})
        calls = []

        def normalize(source, saved_registry, output, **kwargs):
            self.assertEqual(json.loads(saved_registry.read_text()), registry)
            self.assertTrue(kwargs["use_model"])
            calls.append("normalize")
            return {"registry": normalized, "patch": patch_path, "report": {"counts": {"resolved": 2}}}

        def save_rules(path):
            calls.append("save_rules")
            return {"created_rules": 2}

        def save_knowledge(*args, **kwargs):
            calls.append("save_knowledge")
            return SimpleNamespace(returncode=0, stdout=b'{"entity_registry": {"states": {"resolved": 2}}}', stderr=b'')

        with (patch("scripts.export_local_entity_registry.snapshot", return_value=registry) as current,
              patch("scripts.normalize_knowledge_entities.run_normalization", side_effect=normalize),
              patch("scripts.normalize_knowledge_entities.import_patch", side_effect=save_rules),
              patch("scripts.run_literature_batch.subprocess.run", side_effect=save_knowledge),
              patch("apps.curation.knowledge_target_mapping.load_gene_standard", return_value=fixture.gene_standard)):
            result = deliver_batch(batch, dictionary, normalize_entities=True, normalization_model=True, import_to_local=True)
        current.assert_called_once()
        self.assertEqual(calls, ["normalize", "save_rules", "save_knowledge"])
        self.assertEqual(result["entity_normalization"], {"resolved": 2})
        self.assertTrue((batch / "final/entity-normalization.json").exists())


if __name__ == "__main__":
    unittest.main()
