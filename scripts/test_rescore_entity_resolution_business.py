import copy
import unittest

from scripts.rescore_entity_resolution_business import (
    explicit_main_map,
    score_disease,
    summarize_disease,
)


class BusinessAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.item = {"id": "E1", "kind": "disease", "label": "mCRPC", "cohort": "unresolved_after_rules",
                     "candidates": [{"id": "prostate"}, {"id": "lung"}, {"id": "subtype"}],
                     "main_cancer_candidates": [{"id": "prostate"}, {"id": "lung"}]}
        self.reference = {"accepted": [{"decision": "MATCH", "candidate_id": "prostate",
                                        "main_cancer_id": "prostate", "subtype_candidate_id": None}],
                          "required_qualifiers": [["metastatic"]]}
        self.old = {"raw_status": "classified", "prediction": {"decision": "NEW", "candidate_id": None,
                    "main_cancer_id": "prostate", "subtype_candidate_id": None, "reason": "Prostate cancer with qualifiers."},
                    "qualifiers_correct": False, "protocol_valid": True, "all_correct": False,
                    "missing_qualifiers": [["metastatic"]], "identity_correct": False, "seconds": 2.0}
        self.mapping = {"prostate": "prostate", "lung": "lung", "subtype": "prostate"}

    def score(self, **prediction):
        old = copy.deepcopy(self.old)
        old["prediction"].update(prediction)
        return score_disease(self.item, self.reference, old, self.mapping)

    def test_new_and_missing_qualifiers_do_not_fail_correct_main(self):
        row = self.score()
        self.assertTrue(row["direct_main_correct"])
        self.assertFalse(row["extended_fields"]["required_qualifiers_present"])

    def test_missing_duplicate_subtype_is_completeness_only(self):
        self.reference["accepted"][0]["subtype_candidate_id"] = "subtype"
        row = self.score(candidate_id="subtype", decision="MATCH")
        self.assertTrue(row["direct_main_correct"])
        self.assertFalse(row["extended_fields"]["subtype_matches_reference"])

    def test_recovery_is_explicit_and_not_direct_accuracy(self):
        row = self.score(main_cancer_id=None, candidate_id="subtype", decision="MATCH")
        self.assertFalse(row["direct_main_correct"])
        self.assertTrue(row["script_recovered"])
        self.assertTrue(row["business_correct"])

    def test_explanation_does_not_rescue_missing_fields(self):
        row = self.score(main_cancer_id=None, reason="The supplied prostate ID is the correct main cancer.")
        self.assertFalse(row["direct_main_correct"])
        self.assertFalse(row["script_recovered"])
        self.assertTrue(row["explanation_only_main_id_mention"])

    def test_wrong_explicit_main_cannot_be_overwritten_by_candidate(self):
        row = self.score(main_cancer_id="lung", candidate_id="subtype", decision="MATCH")
        self.assertFalse(row["business_correct"])
        self.assertIn("wrong_main_cancer", row["failures"])
        self.assertIn("main_candidate_conflict", row["failures"])

    def test_correct_main_with_contradictory_candidate_fails(self):
        row = self.score(candidate_id="lung", decision="MATCH")
        self.assertFalse(row["direct_main_correct"])
        self.assertIn("candidate_points_to_wrong_main", row["failures"])

    def test_not_entity_does_not_pass_even_with_right_main(self):
        row = self.score(decision="NOT_ENTITY")
        self.assertFalse(row["business_correct"])
        self.assertIn("disease_rejected_as_non_entity", row["failures"])

    def test_not_entity_is_not_rescued(self):
        self.assertFalse(self.score(decision="NOT_ENTITY", candidate_id="subtype", main_cancer_id=None)["script_recovered"])

    def test_unknown_candidate_cannot_be_used_for_recovery(self):
        self.assertFalse(self.score(candidate_id="invented", main_cancer_id=None)["script_recovered"])

    def test_invalid_main_type_is_reported(self):
        self.assertIn("invalid_main_id", self.score(main_cancer_id=["prostate"])["failures"])

    def test_subtype_pointing_to_other_main_is_contradiction(self):
        self.assertFalse(self.score(subtype_candidate_id="lung")["business_correct"])

    def test_no_model_output_fails(self):
        self.old["raw_status"] = "output_truncated"
        self.assertFalse(self.score()["business_correct"])

    def test_separate_denominators_and_recovery_counts(self):
        rows = [self.score(), self.score(main_cancer_id=None, candidate_id="subtype", decision="MATCH")]
        summary = summarize_disease(rows)
        self.assertEqual(summary["direct_main_correct"], 1)
        self.assertEqual(summary["script_recovered"], 1)
        self.assertEqual(summary["direct_main_accuracy"], 0.5)
        self.assertEqual(summary["extended_fields"]["subtype_matches_reference_applicable"], 0)

    def test_frozen_reference_mapping_rejects_conflicting_main(self):
        second = copy.deepcopy(self.reference)
        second["accepted"][0]["main_cancer_id"] = "lung"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            explicit_main_map([self.item, {**self.item, "id": "E2"}], {"records": [self.reference, second]})


if __name__ == "__main__":
    unittest.main()
