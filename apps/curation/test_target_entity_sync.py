import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from apps.curation import test_knowledge_target_mapping as mapping_tests
from apps.curation.knowledge_export import export_bundle, validate_target_payload
from apps.curation.knowledge_target_mapping import prepare_mappings
from apps.curation.target_entity_sync import (
    PreviewTarget,
    operation_index,
    preview_entity_sync,
)


class TargetEntitySyncTests(unittest.TestCase):
    def setUp(self):
        fixture = mapping_tests.KnowledgeTargetMappingTests()
        fixture.setUp()
        self.bundle = fixture.bundle
        self.dictionary = fixture.dictionary
        self.dictionary["drugs"][0]["name"] = "Unrelated existing drug"
        self.registry = fixture.registry()
        self.registry["entities"][0]["external_ids"] = {"RXNORM": "123"}
        self.genes = fixture.gene_standard
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "isolated-preview.sqlite3"

    def plan(self):
        mappings, _ = prepare_mappings(self.bundle, self.dictionary, self.registry, gene_standard=self.genes)
        return mappings, export_bundle(self.bundle, dictionary=self.dictionary, mappings=mappings)

    def test_shared_drug_created_once_then_ids_bound_without_changing_knowledge(self):
        second = copy.deepcopy(self.bundle["records"][0])
        second["group_id"] = "record-2"
        self.bundle["records"].append(second)
        original = copy.deepcopy(self.bundle)
        mappings, package = self.plan()
        self.assertEqual(len(package["entity_operations"]), 1)
        self.assertEqual(package["ready_after_entity_sync_count"], 2)
        target = PreviewTarget(self.path, self.dictionary)
        self.addCleanup(target.close)
        bound, receipt = preview_entity_sync(mappings, target)
        self.assertEqual((receipt["created_count"], receipt["reused_count"]), (1, 0))
        new_id = receipt["operations"][0]["target_id"]
        self.assertTrue(all(row["entity_ids"]["drug"] == new_id for row in bound.values()))
        exported = export_bundle(self.bundle, dictionary=target.snapshot(), mappings=bound)
        self.assertEqual(exported["preview_ready_count"], 2)
        self.assertEqual(exported["offline_ready_count"], 0)
        self.assertTrue(exported["preview_only"])
        self.assertFalse(exported["target_written"])
        self.assertTrue(all(row["compatibility_status"] == "preview_validated" for row in exported["records"]))
        self.assertEqual([row["biomed_companion"] for row in exported["records"]], original["records"])
        self.assertEqual(self.bundle, original)
        replay, receipt = preview_entity_sync(mappings, target)
        self.assertEqual((receipt["created_count"], receipt["reused_count"]), (0, 1))
        self.assertEqual(replay["record-1"]["entity_ids"]["drug"], new_id)
        self.assertEqual(len(target.snapshot()["drugs"]), 2)

    def test_restart_after_drug_commit_before_evidence_binding_reuses_saved_id(self):
        mappings, package = self.plan()
        target = PreviewTarget(self.path, self.dictionary)
        first = target.ensure_drug(package["entity_operations"][0])
        target.close()
        target = PreviewTarget(self.path, self.dictionary)
        self.addCleanup(target.close)
        bound, receipt = preview_entity_sync(mappings, target)
        self.assertEqual(receipt["created_count"], 0)
        self.assertEqual(bound["record-1"]["entity_ids"]["drug"], first["target_id"])

    def test_concurrent_retries_create_one_drug(self):
        _, package = self.plan()
        PreviewTarget(self.path, self.dictionary).close()

        def execute(_):
            target = PreviewTarget(self.path, self.dictionary)
            try:
                return target.ensure_drug(package["entity_operations"][0])
            finally:
                target.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(execute, range(3)))
        self.assertEqual(sum(row["action"] == "created" for row in results), 1)
        self.assertEqual(len({row["target_id"] for row in results}), 1)

    def test_target_added_after_snapshot_is_reused_by_full_name(self):
        mappings, _ = self.plan()
        updated = copy.deepcopy(self.dictionary)
        updated["drugs"].append({"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "name": "canonical drug"})
        target = PreviewTarget(self.path, updated)
        self.addCleanup(target.close)
        bound, receipt = preview_entity_sync(mappings, target)
        self.assertEqual(receipt["created_count"], 0)
        self.assertEqual(receipt["operations"][0]["action"], "reused_name")
        self.assertEqual(bound["record-1"]["entity_ids"]["drug"], updated["drugs"][-1]["id"])

    def test_ambiguous_target_name_is_isolated_and_never_arbitrarily_merged(self):
        mappings, _ = self.plan()
        updated = copy.deepcopy(self.dictionary)
        for name, suffix in (("canonical drug", "a"), ("Canonical Drug", "b")):
            updated["drugs"].append({"id": f"{suffix * 8}-{suffix * 4}-4{suffix * 3}-8{suffix * 3}-{suffix * 12}", "name": name})
        target = PreviewTarget(self.path, updated)
        self.addCleanup(target.close)
        bound, receipt = preview_entity_sync(mappings, target)
        self.assertEqual(len(receipt["failures"]), 1)
        self.assertEqual(receipt["created_count"], 0)
        exported = export_bundle(self.bundle, dictionary=target.snapshot(), mappings=bound)
        self.assertEqual(exported["preview_ready_count"], 0)
        self.assertIn("target_entity_sync_failed", {row["code"] for row in exported["records"][0]["mapping_issues"]})
        self.assertEqual(len(target.snapshot()["drugs"]), 3)

    def test_replay_cannot_replace_previously_bound_identity(self):
        mappings, _ = self.plan()
        target = PreviewTarget(self.path, self.dictionary)
        self.addCleanup(target.close)
        preview_entity_sync(mappings, target)
        self.registry["entities"][0]["external_ids"]["RXNORM"] = "conflicting-id"
        changed, _ = self.plan()
        _, receipt = preview_entity_sync(changed, target)
        self.assertEqual(len(receipt["failures"]), 1)
        self.assertEqual(len(target.snapshot()["drugs"]), 2)

    def test_confirmed_global_alias_reuses_target_and_article_alias_is_not_globalized(self):
        self.dictionary["drugs"][0]["name"] = "Drug A"
        mappings, package = self.plan()
        self.assertEqual(package["entity_operations"], [])
        self.assertEqual(mappings["record-1"]["entity_ids"]["drug"], self.dictionary["drugs"][0]["id"])
        self.registry["rules"][0]["scope"] = {"type": "article", "key": self.bundle["records"][0]["version_id"]}
        _, package = self.plan()
        self.assertEqual(package["entity_operations"][0]["local_identity"]["confirmed_global_aliases"], [])

    def test_additional_confirmed_alias_keeps_the_saved_target_id(self):
        mappings, _ = self.plan()
        target = PreviewTarget(self.path, self.dictionary)
        self.addCleanup(target.close)
        bound, _ = preview_entity_sync(mappings, target)
        alias = copy.deepcopy(self.registry["rules"][0])
        alias.update(id="another-alias", alias="Another confirmed spelling")
        self.registry["rules"].append(alias)
        changed, _ = self.plan()
        again, receipt = preview_entity_sync(changed, target)
        self.assertEqual(receipt["failures"], {})
        self.assertEqual(receipt["created_count"], 0)
        self.assertEqual(again["record-1"]["entity_ids"]["drug"], bound["record-1"]["entity_ids"]["drug"])

    def test_unconfirmed_or_wrong_kind_drugs_never_create_operations(self):
        original = copy.deepcopy(self.registry)
        for change in ("unconfirmed", "class", "candidate", "overlong", "unanchored"):
            with self.subTest(change=change):
                self.registry = copy.deepcopy(original)
                if change == "unconfirmed":
                    self.registry["rules"][0]["status"] = "proposed"
                elif change == "class":
                    self.registry["entities"][0]["provenance"]["entity_level"] = "drug_class"
                elif change == "candidate":
                    self.registry["entities"][0]["status"] = "candidate"
                elif change == "overlong":
                    self.registry["entities"][0]["name"] = "x" * 256
                else:
                    anchor = self.bundle["records"][0]["anchors"][0]
                    quote = "No named intervention is provided here."
                    anchor.update(quote=quote, end=len(quote))
                _, package = self.plan()
                self.assertEqual(package["entity_operations"], [])
                self.assertEqual(package["ready_after_entity_sync_count"], 0)

    def test_target_prerequisites_do_not_bypass_other_evidence_gaps(self):
        self.bundle["records"][0]["fact"]["conditions"].append("ctDNA clearance")
        _, package = self.plan()
        self.assertEqual(len(package["entity_operations"]), 1)
        self.assertEqual(package["ready_after_entity_sync_count"], 0)
        self.assertFalse(package["records"][0]["offline_ready"])

    def test_template_reference_is_not_a_uuid_or_valid_plain_api_payload(self):
        _, package = self.plan()
        row = package["records"][0]
        template = row["target_payload_template"]
        self.assertFalse(validate_target_payload(template, self.dictionary)["valid"])
        self.assertTrue(validate_target_payload(template, self.dictionary, entity_operations=row["entity_operations"])["valid"])
        template["evidence_category"] = "diagnostic"
        result = validate_target_payload(template, self.dictionary, entity_operations=row["entity_operations"])
        self.assertIn("treatment_only", {error["code"] for error in result["errors"]})

    def test_modified_operations_and_nonpreview_database_are_rejected(self):
        _, package = self.plan()
        operation = package["entity_operations"][0]
        operation["create_payload"]["name_cn"] = "Injected different drug"
        with self.assertRaises(ValueError):
            operation_index([operation])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE existing_knowledge (id TEXT)")
        with self.assertRaisesRegex(ValueError, "non-preview"):
            PreviewTarget(self.path, self.dictionary)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("existing_knowledge",)])


if __name__ == "__main__":
    unittest.main()
