import unittest

from apps.curation.evidence_scope_context import compare_reported_context


class ReportedContextComparisonTests(unittest.TestCase):
    def setUp(self):
        self.requirements = [{"field": "therapy_line", "expected": "first-line therapy",
                              "known_values": ["first-line therapy", "second-line therapy"],
                              "source": {"pmid": "fixture", "passage": "P5"}}]

    def test_different_line_is_marked_as_different_without_hiding_the_knowledge(self):
        result = compare_reported_context(self.requirements, {"therapy_line": "second-line therapy"})
        self.assertEqual(result["status"], "different_from_reported_context")
        self.assertTrue(result["informational_retrieval"])
        self.assertFalse(result["determines_patient_eligibility"])

    def test_unknown_is_not_treated_as_a_match_or_a_contradiction(self):
        for observed in ({}, {"therapy_line": ""}, {"therapy_line": "unknown"},
                         {"therapy_line": "未确定"}, {"therapy_line": "1L"}):
            with self.subTest(observed=observed):
                self.assertEqual(compare_reported_context(self.requirements, observed)["status"], "unknown_context")

    def test_normalized_matching_and_unrelated_regimen_do_not_add_a_condition(self):
        result = compare_reported_context(self.requirements, {"therapy_line": "  FIRST-LINE   THERAPY  ",
                                                               "dose": "different or not yet prescribed"})
        self.assertEqual(result["status"], "consistent_with_reported_context")
        self.assertEqual(len(result["checks"]), 1)
        self.assertFalse(result["treatment_recommendation"])

    def test_missing_source_duplicate_field_and_malformed_values_fail_closed(self):
        for requirements, observed in (([{"field": "x", "expected": "a"}], {}),
                                       (self.requirements * 2, {}),
                                       (self.requirements, {"therapy_line": 1})):
            with self.subTest(requirements=requirements, observed=observed), self.assertRaises(ValueError):
                compare_reported_context(requirements, observed)


if __name__ == "__main__":
    unittest.main()
