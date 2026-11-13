import unittest

from apps.curation.extraction_outcome_diagnostics import diagnose


class ExtractionOutcomeDiagnosticTests(unittest.TestCase):
    def test_truncated_entity_output_is_not_an_empty_fact(self):
        result = diagnose({"status": "output_truncated", "payload": None}, None)
        self.assertEqual(result["outcome"], "entity_output_truncated")
        self.assertEqual(result["next_action"], "bounded_retry_with_full_source_and_new_version")
        self.assertFalse(result["automatic_retry_started"])

    def test_valid_empty_relation_is_not_a_model_failure_or_proof_of_absence(self):
        entity = {"status": "classified", "payload": {"entities": [{"text": "drug"}]}}
        relation = {"status": "classified", "payload": {"facts": [], "reason": "none found"}}
        result = diagnose(entity, relation)
        self.assertEqual(result["outcome"], "model_returned_no_fact")
        self.assertFalse(result["model_no_fact_proves_absence"])

    def test_missing_or_invalid_results_do_not_become_no_fact(self):
        entity = {"status": "classified", "payload": {"entities": []}}
        self.assertEqual(diagnose(entity, None)["outcome"], "relation_result_missing")
        self.assertEqual(diagnose(entity, {"status": "classified", "payload": {}})["outcome"], "relation_output_invalid")
        self.assertEqual(diagnose(entity, {"status": "output_truncated"})["outcome"], "relation_output_truncated")
