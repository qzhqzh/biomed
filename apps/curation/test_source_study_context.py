import unittest

from apps.curation.source_study_context import assess, guard


class SourceStudyContextTests(unittest.TestCase):
    def setUp(self):
        self.sample = {"source_document": {"metadata": {"publication_types": ["Journal Article"]}},
                       "record": {"group_id": "g", "fact": {"population": "patients", "results": []},
                                  "anchors": [{"purpose": "evidence", "quote": "patients received treatment"}]}}

    def test_review_metadata_blocks_new_primary_assertion_without_rewriting_citations(self):
        self.sample["source_document"]["metadata"]["publication_types"].append("Review")
        result = guard(self.sample, {"status": "offline_structure_only", "reason": "x", "linked_fact": {"source_scope": "primary"}})
        self.assertEqual(result["reason"], "review_publication_conflicts_with_primary_assertion")
        self.assertEqual(result["linked_fact"]["source_scope"], "primary")
        self.assertEqual(result["status"], "deferred")

    def test_review_does_not_block_previous_baseline_or_invent_a_scope(self):
        self.sample["source_document"]["metadata"]["publication_types"].append("Review")
        result = guard(self.sample, {"status": "baseline_preserved", "linked_fact": {"source_scope": "primary"}})
        self.assertEqual(result["status"], "baseline_preserved")
        self.assertFalse(assess(self.sample, {"source_scope": "cited_work"})["review_primary_conflict"])

    def test_pdx_population_requires_an_explicit_source_mention(self):
        self.sample["record"]["fact"]["population"] = "osteosarcoma PDX model"
        self.assertFalse(assess(self.sample)["preclinical_population_explicit_in_fact_and_evidence"])
        self.sample["record"]["anchors"][0]["quote"] = "treatment in an osteosarcoma PDX model"
        self.assertTrue(assess(self.sample)["preclinical_population_explicit_in_fact_and_evidence"])

    def test_treatment_receipt_does_not_become_a_treatment_response(self):
        self.sample["record"]["fact"]["results"] = [{"measure": "proportion of patients receiving treatment"}]
        context = assess(self.sample)
        self.assertTrue(context["result_describes_treatment_receipt"])
        self.assertFalse(context["automatic_clinical_matching"])
