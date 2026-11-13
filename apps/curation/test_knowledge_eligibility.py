import copy
import unittest

from apps.curation.knowledge_eligibility import anchor_eligibility, assess_record


class KnowledgeEligibilityTests(unittest.TestCase):
    def test_glossary_metadata_is_used_without_a_short_text_heuristic(self):
        self.assertEqual(anchor_eligibility({"text": "Drug A works."})["status"], "eligible")
        self.assertEqual(anchor_eligibility({"ancestor_tags": ["def", "def-item", "def-list"]})["reasons"],
                         ["glossary_context_only"])

    def test_methods_and_context_only_are_deferred_without_changing_source(self):
        record = {"fact": {"relation": "other", "population": "colorectal cancer", "treatment": "bevacizumab",
                           "conditions": ["pre-treatment serum levels measured by ELISA"], "results": [],
                           "entity_roles": {"d": ["disease"], "a": ["intervention"]}},
                  "assessment": {"execution": {"status": "complete"}, "quality": {"status": "unreviewed"}}}
        old = copy.deepcopy(record)
        self.assertEqual(assess_record(record)["reasons"], ["no_core_relation"])
        self.assertEqual(record, old)
        record["fact"]["entity_roles"] = {"a": ["context"]}
        self.assertEqual(assess_record(record)["reasons"], ["no_core_relation", "no_located_participants"])

    def test_qualitative_and_recommendation_do_not_require_numbers_or_subtypes(self):
        for relation, conclusion in (("recommendation", None), ("mechanism", None),
                                     ("treatment_outcome", None), ("other", "No benefit was observed.")):
            record = {"fact": {"relation": relation, "conclusion": conclusion, "results": [],
                               "entity_roles": {"a": ["intervention"]}}}
            with self.subTest(relation=relation):
                self.assertEqual(assess_record(record)["status"], "eligible")

    def test_wrong_center_and_missing_fact_remain_deferred(self):
        self.assertEqual(assess_record({"fact": {}})["reasons"], ["no_extracted_fact"])
        record = {"fact": {"relation": "mechanism", "entity_roles": {"a": ["molecular"]}},
                  "assessment": {"execution": {"issues": ["target_anchor_not_cited"]}}}
        self.assertEqual(assess_record(record)["reasons"], ["target_anchor_not_cited"])
        result = assess_record(record, passages=[{"role": "anchor", "ancestor_tags": ["def-list"]}])
        self.assertTrue(result["structure_assessed"])
        self.assertEqual(result["reasons"], ["glossary_context_only", "target_anchor_not_cited"])

    def test_legacy_missing_role_map_is_not_assumed_empty(self):
        result = assess_record({"fact": {"relation": "biomarker_association", "population": "lung cancer"}})
        self.assertEqual(result["status"], "eligible")
        self.assertFalse(result["participants_assessed"])
        self.assertFalse(result["structure_assessed"])
        self.assertEqual(result["semantic_correctness"], "unassessed")

    def test_only_observed_method_descriptions_are_deferred(self):
        record = {"fact": {"relation": "biomarker_association", "results": [
            {"measure": "LRR and DR scores", "value": "p-values derived from Wilcoxon rank-sum tests"},
            {"measure": "ROC curves", "value": "exploratory ROC analysis"},
            {"measure": "Nodal status", "value": "exploratory ROC analysis"},
        ]}}
        original = copy.deepcopy(record)
        self.assertEqual(assess_record(record)["reasons"], ["methods_only_results"])
        self.assertEqual(assess_record(record)["version"], "knowledge-structural-eligibility-v2")
        self.assertEqual(record, original)

    def test_qualitative_numeric_and_mixed_findings_are_not_method_only(self):
        for value in ("no significant difference", "response improved", "P = 0.02", "AUC = 0.81",
                      "exploratory ROC analysis: AUC = 0.81", "p-values derived from Wilcoxon rank-sum tests: p = 0.02"):
            record = {"fact": {"relation": "biomarker_association", "results": [
                {"value": "exploratory ROC analysis"}, {"value": value},
            ]}}
            with self.subTest(value=value):
                self.assertEqual(assess_record(record)["status"], "eligible")

    def test_explicit_conclusion_or_qualitative_relation_is_not_method_only(self):
        for relation, conclusion in (("biomarker_association", "Higher DR scores predicted poorer response."),
                                     ("recommendation", None), ("mechanism", None)):
            record = {"fact": {"relation": relation, "conclusion": conclusion,
                               "results": [{"value": "exploratory ROC analysis"}]}}
            with self.subTest(relation=relation):
                self.assertEqual(assess_record(record)["status"], "eligible")


if __name__ == "__main__":
    unittest.main()
