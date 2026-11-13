import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.publish_target_compatibility import (
    build_batch,
    digest,
    file_sha,
    quality_from_receipt,
)


class TargetCompatibilityPublicationTests(unittest.TestCase):
    def test_existing_quality_binding_preserves_target_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bundle.json"
            path.write_text(json.dumps({"records": []}))
            quality = {"review_run": "real-run", "model": "astra", "records": [{
                "candidate_id": "stored", "decision": "pass", "reason": "Valid extra fact",
                "quality_dimensions": {"core_valid": True, "target_covered": False, "participants_usable": True},
            }]}
            receipt = {"applied": True, "bundle_sha256": file_sha(path),
                       "quality_input_sha256": digest(quality), "quality": {"review_run": "real-run"},
                       "intake": {"candidates": [{"id": "stored", "group_id": "G"}]}}
            result = quality_from_receipt(path, receipt, quality)
            self.assertEqual(result["G"]["status"], "valid")
            self.assertFalse(result["G"]["quality_dimensions"]["target_covered"])
            changed = copy.deepcopy(quality)
            changed["records"][0]["decision"] = "fail"
            with self.assertRaisesRegex(ValueError, "quality SHA"):
                quality_from_receipt(path, receipt, changed)
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "bundle SHA"):
                quality_from_receipt(path, receipt, quality)

    def fixtures(self):
        row = {"group_id": "G", "article": {"pmid": "1", "title": "Article"}, "fact": {"relation": "treatment"},
               "anchors": [{"purpose": "evidence", "passage_id": "p", "locator": "body", "start": 0, "end": 5, "quote": "quote"}]}
        bundle = {"source_run": "run", "records": [row]}
        dictionary = {"drugs": [{"id": "real-id", "name": "Drug"}], "tumors": []}
        exported = {"source_run": "run", "source_bundle_sha256": digest(bundle),
                    "target_dictionary": {"sha256": digest(dictionary)}, "target_written": False, "submit_ready": False,
                    "records": [{"group_id": "G", "biomed_companion": row, "offline_ready": False,
                                 "compatibility_status": "pending_mapping", "resolved_entities": {"e": {"target_id": "real-id"}},
                                 "target_payload": {}, "mapping_issues": [], "contract_validation": {}}]}
        receipt = {"applied": {"source_run": "run", "dry_run": False, "import_sha256": digest(bundle),
                               "candidates": [{"group_id": "G", "id": "stored-id"}]}}
        notes = {("run", "G"): {"entity_mappings": [{"entity_ref": "e", "target_id": "real-id"}],
                                 "trigger_scope": {}, "treatment_scope": {}, "review_points": []}}
        return bundle, exported, receipt, notes, dictionary

    def test_unreviewed_mapping_does_not_invent_quality(self):
        args = self.fixtures()
        row = build_batch(*args)[0]
        self.assertEqual(row["quality"]["status"], "unreviewed")
        self.assertEqual(row["candidate_id"], "stored-id")
        self.assertEqual(row["entity_mappings"][0]["target_name"], "Drug")
        self.assertTrue(row["entity_mappings"][0]["accepted_by_exporter"])

    def test_rejects_forged_target_mapping_or_dictionary(self):
        args = self.fixtures()
        args[3][("run", "G")]["entity_mappings"][0]["target_id"] = "invented"
        with self.assertRaisesRegex(ValueError, "absent from dictionary"):
            build_batch(*args)
        args = self.fixtures()
        args[4]["drugs"].append({"id": "second", "name": "Other"})
        with self.assertRaisesRegex(ValueError, "dictionary snapshot differs"):
            build_batch(*args)

    def test_rejects_modified_source_and_target_write(self):
        args = self.fixtures()
        args[1]["target_written"] = True
        with self.assertRaisesRegex(ValueError, "not written"):
            build_batch(*args)
        args = self.fixtures()
        args[1]["records"][0]["biomed_companion"] = {"fact": "different"}
        with self.assertRaisesRegex(ValueError, "companion differs"):
            build_batch(*args)


if __name__ == "__main__":
    unittest.main()
