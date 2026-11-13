import copy
import unittest

from scripts.benchmark_entity_resolution import check_prediction, score_row
from scripts.prepare_entity_resolution import decide_rule


class EntityResolutionRulesTests(unittest.TestCase):
    def test_generic_retinoic_acid_is_not_tretinoin_by_synonym(self):
        candidates = [{"id": "std:CHEMBL:CHEMBL38", "namespace": "CHEMBL", "label": "TRETINOIN"}]
        self.assertIsNone(decide_rule("drug", "retinoic acid", [], [], candidates))

    def test_similar_or_component_seed_names_are_not_exact(self):
        targets = [{"id": "seed:1", "label": "cisplatin", "aliases": []}]
        self.assertIsNone(decide_rule("drug", "cisplatin plus gemcitabine", [], targets, []))
        self.assertIsNone(decide_rule("drug", "cisplatine", [], targets, []))

    def test_source_definition_required_for_onc201_rxnorm_link(self):
        standards = [{"id": "std:RXNORM:2721194", "namespace": "RXNORM", "label": "dordaviprone"}]
        self.assertIsNone(decide_rule("drug", "ONC201", [], [], standards))
        mentions = [{"source_support": [{"quote": "Treatment with dordaviprone (ONC201) was assessed."}]}]
        result = decide_rule("drug", "ONC201", mentions, [], standards)
        self.assertEqual(result["candidate_id"], "std:RXNORM:2721194")
        self.assertEqual(result["method"], "explicit_document_alias")

    def test_dmg_business_mapping_preserves_subtype_not_pontine(self):
        targets = [{"id": "seed:dmg", "label": "DMG", "name_cn": "弥漫性中线胶质瘤"},
                   {"id": "seed:glioma", "label": "Glioma", "name_cn": "脑胶质瘤"}]
        result = decide_rule("disease", "Diffuse midline glioma", [], targets, [])
        self.assertEqual(result["candidate_id"], "seed:dmg")
        self.assertEqual(result["main_cancer_id"], "seed:glioma")
        self.assertEqual(result["subtype_candidate_id"], "seed:dmg")

    def test_qualified_disease_does_not_become_parent_alias_rule(self):
        targets = [{"id": "seed:breast", "label": "breast cancer", "aliases": []}]
        self.assertIsNone(decide_rule("disease", "HER2-positive metastatic breast cancer", [], targets, []))


class EntityResolutionScoringTests(unittest.TestCase):
    def setUp(self):
        self.item = {"id": "E1", "cohort": "unresolved_after_rules", "label": "advanced breast cancer", "kind": "disease",
                     "candidates": [{"id": "seed:breast"}, {"id": "seed:lung"}],
                     "main_cancer_candidates": [{"id": "seed:breast"}],
                     "contexts": [{"quote": "The trial enrolled advanced breast cancer patients."}]}
        self.value = {"decision": "MATCH", "candidate_id": "seed:breast", "match_relation": "qualified_form",
                      "main_cancer_id": "seed:breast", "subtype_candidate_id": None,
                      "qualifiers": ["advanced"], "evidence_quote": "advanced breast cancer", "reason": "Core disease and source qualifier."}
        self.reference = {"accepted": [{k: self.value[k] for k in
                                       ("decision", "candidate_id", "match_relation", "main_cancer_id", "subtype_candidate_id")}],
                          "required_qualifiers": [["advanced"]]}

    def score(self, value):
        return score_row(self.item, self.reference, {"status": "classified", "payload": value})

    def test_valid_result(self):
        self.assertTrue(self.score(self.value)["all_correct"])

    def test_quote_error_is_not_false_identity_merge(self):
        value = {**self.value, "evidence_quote": "A paraphrase not supplied"}
        row = self.score(value)
        self.assertFalse(row["protocol_valid"])
        self.assertTrue(row["identity_correct"])
        self.assertFalse(row["false_merge"])
        self.assertFalse(row["all_correct"])

    def test_invented_id_is_rejected(self):
        value = {**self.value, "candidate_id": "hallucinated"}
        self.assertIn("invented_or_missing_candidate", check_prediction(self.item, value))
        self.assertTrue(self.score(value)["false_merge"])

    def test_json_array_candidate_is_reported_not_crash(self):
        value = {**self.value, "candidate_id": ["seed:breast"]}
        self.assertEqual(check_prediction(self.item, value), ["invalid_candidate_type"])
        self.assertFalse(self.score(value)["protocol_valid"])

    def test_json_object_decision_is_reported_not_crash(self):
        row = self.score({**self.value, "decision": {"value": "MATCH"}})
        self.assertEqual(row["protocol_errors"], ["invalid_decision_type"])

    def test_missing_fields_do_not_match_null_reference(self):
        value = {k: v for k, v in self.value.items() if k != "subtype_candidate_id"}
        self.assertFalse(self.score(value)["classification_correct"])

    def test_wrong_candidate_counts_false_merge(self):
        self.assertTrue(self.score({**self.value, "candidate_id": "seed:lung"})["false_merge"])

    def test_wrong_relation_is_not_false_identity_merge(self):
        row = self.score({**self.value, "match_relation": "alias"})
        self.assertFalse(row["identity_correct"])
        self.assertFalse(row["false_merge"])

    def test_missing_qualifier_is_separate_from_identity(self):
        row = self.score({**self.value, "qualifiers": []})
        self.assertTrue(row["identity_correct"])
        self.assertFalse(row["qualifiers_correct"])
        self.assertFalse(row["all_correct"])

    def test_unresolved_counts_missed_existing_without_merge(self):
        value = {**self.value, "decision": "UNRESOLVED", "candidate_id": None, "match_relation": "unresolved"}
        row = self.score(value)
        self.assertTrue(row["missed_existing_identity"])
        self.assertFalse(row["false_merge"])

    def test_absent_result_never_scores_null_reference_as_correct(self):
        ref = copy.deepcopy(self.reference)
        ref["accepted"][0].update(main_cancer_id=None, subtype_candidate_id=None)
        row = score_row(self.item, ref, {})
        self.assertFalse(row["classification_correct"])
        self.assertFalse(row["identity_correct"])


if __name__ == "__main__":
    unittest.main()
