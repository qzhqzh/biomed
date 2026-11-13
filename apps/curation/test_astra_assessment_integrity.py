import copy
import unittest

from apps.curation.astra_assessment_integrity import validate


class AstraAssessmentIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.source = {"articles": [{"pmid": "1", "passages": [{"id": "p", "locator": "body/p:1", "text": "A source sentence."}]}]}
        self.comparison = {"records": [{"pmid": "1", "group_id": "g", "new_preview": {"status": "offline_structure_only"}}]}
        self.assessment = {"reviewer": "gpt-6-astra", "reference_type": "source_only_reference_based_offline_semantic_assessment_not_expert_gold",
                           "records": [{"pmid": "1", "group_id": "g", "new_status": "offline_structure_only",
                                        "core_judgment": "supported", "evidence": [{"passage_id": "p", "locator": "body/p:1", "quote": "source sentence"}]}]}

    def test_valid_quote_and_frozen_candidate(self):
        result = validate(self.source, self.comparison, self.assessment, expected_pmids={"1"})
        self.assertEqual(result["judgments"]["supported"], 1)
        self.assertFalse(result["semantic_correctness_proved"])

    def test_changed_group_or_invented_quote_fails(self):
        assessment = copy.deepcopy(self.assessment)
        assessment["records"][0]["group_id"] = "another"
        with self.assertRaisesRegex(ValueError, "candidate differs"):
            validate(self.source, self.comparison, assessment, expected_pmids={"1"})
        assessment = copy.deepcopy(self.assessment)
        assessment["records"][0]["evidence"][0]["quote"] = "invented"
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate(self.source, self.comparison, assessment, expected_pmids={"1"})
