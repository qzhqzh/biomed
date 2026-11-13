import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_linked_facts import normalize

VERSION_ID = "44444444-4444-4444-8444-444444444444"
PASSAGE_1 = "55555555-5555-4555-8555-555555555555"
PASSAGE_2 = "66666666-6666-4666-8666-666666666666"


class SourceLinkedFactTests(unittest.TestCase):
    def setUp(self):
        passages = [
            {"id": PASSAGE_1, "version_id": VERSION_ID, "locator": "body/p:1",
             "text": "Patients with Cancer X harboring TEST V1A received Drug A."},
            {"id": PASSAGE_2, "version_id": VERSION_ID, "locator": "body/p:2",
             "text": "The treated patients had a partial response."},
        ]
        anchors = [{"passage_id": p["id"], "start": 0, "end": len(p["text"]), "quote": p["text"],
                    "purpose": "evidence", "attributes": {"alias": f"P{i}"}}
                   for i, p in enumerate(passages, 1)]
        self.sample = {
            "pmid": "fixture",
            "record": {"group_id": "G1", "version_id": VERSION_ID, "source_sha256": "a" * 64,
                       "entities": [{"ref": "disease", "kind": "disease", "label": "Cancer X"},
                                    {"ref": "variant", "kind": "variant", "label": "TEST V1A"},
                                    {"ref": "drug", "kind": "drug", "label": "Drug A"}],
                       "anchors": anchors},
            "source_document": {"fulltext_version_id": VERSION_ID,
                                "versions": [{"id": VERSION_ID, "sha256": "a" * 64}],
                                "passages": passages, "passages_sha256": digest(passages)},
        }
        self.payload = {"facts": [{
            "relation": "treatment_outcome", "source_scope": "primary",
            "population": {"text": "Cancer X patients with TEST V1A", "evidence": ["P1"]},
            "disease": {"text": "Cancer X", "entity_ref": "disease", "evidence": ["P1"]},
            "molecular_event": {"gene": "TEST", "event_type": "mutation", "event": "V1A",
                                "status": "present", "role": "eligibility", "entity_refs": ["variant"],
                                "evidence": ["P1"]},
            "intervention": {"text": "Drug A", "entity_ref": "drug", "role": "current", "evidence": ["P1"]},
            "outcome": {"text": "partial response", "evidence": ["P2"]},
            "same_cohort": True, "cohort_evidence": ["P1"], "conditions": [],
        }], "reason": None}

    def test_keeps_field_level_passages_and_does_not_claim_semantic_validation(self):
        result = normalize(self.sample, self.payload)
        self.assertEqual(result["status"], "source_linked")
        self.assertEqual(result["semantic_status"], "unreviewed")
        self.assertEqual(result["molecular_event"]["evidence"][0]["passage_id"], PASSAGE_1)
        self.assertEqual(result["outcome"]["evidence"][0]["passage_id"], PASSAGE_2)
        self.assertFalse(result["target_written"])

    def test_bad_source_text_or_version_is_rejected(self):
        sample = copy.deepcopy(self.sample)
        sample["record"]["anchors"][0]["quote"] = "invented quote"
        with self.assertRaisesRegex(ValueError, "Evidence alias"):
            normalize(sample, self.payload)
        sample = copy.deepcopy(self.sample)
        sample["record"]["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "content hash"):
            normalize(sample, self.payload)

    def test_unknown_passage_or_wrong_entity_kind_stays_incomplete(self):
        payload = copy.deepcopy(self.payload)
        payload["facts"][0]["molecular_event"]["evidence"] = ["P99"]
        payload["facts"][0]["intervention"]["entity_ref"] = "disease"
        result = normalize(self.sample, payload)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("invalid_evidence_reference", [i["code"] for i in result["issues"]])
        self.assertIn("entity_reference_unresolved", [i["code"] for i in result["issues"]])

    def test_invalid_model_reference_shapes_do_not_create_an_assertion(self):
        payload = copy.deepcopy(self.payload)
        payload["facts"][0]["molecular_event"]["entity_refs"] = [{}]
        result = normalize(self.sample, payload)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("entity_reference_unresolved", [i["code"] for i in result["issues"]])


if __name__ == "__main__":
    unittest.main()
