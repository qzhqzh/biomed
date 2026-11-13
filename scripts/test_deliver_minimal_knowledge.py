import copy
import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase

from scripts.deliver_minimal_knowledge import (
    exact_quote,
    prepare_delivery,
    quality_record,
    selected_reviews,
)


class MinimalDeliveryTests(SimpleTestCase):
    def test_prepare_retains_all_knowledge_without_semantic_gate(self):
        source = {
            "schema_version": "knowledge-candidates-v1", "source_run": "frozen-input",
            "records": [{"group_id": "K01", "fact": {"treatment": "fixture"},
                         "entities": [], "anchors": [], "assessment": {"quality": {"status": "invalid"}}}],
        }
        original = copy.deepcopy(source)
        bundle, exported = prepare_delivery(source, "a" * 64)
        self.assertEqual(source, original)
        self.assertEqual(len(bundle["records"]), 1)
        self.assertEqual(bundle["records"][0]["fact"], original["records"][0]["fact"])
        self.assertEqual(bundle["records"][0]["assessment"]["quality"]["status"], "unreviewed")
        self.assertFalse(exported["submit_ready"])
        self.assertEqual(exported["records"][0]["biomed_companion"], bundle["records"][0])

    def test_selected_review_checks_file_chain_and_preserves_target_coverage(self):
        source = {"records": [{"group_id": "K01"}]}
        review = {"core_valid": True, "target_covered": False, "participants_usable": True}
        final = {"bundle_sha256": {"A": "bundle"}, "prior_review_sha256": {"A": "prior"},
                 "records": [{"id": "K01", "arms": {"A": review}}]}
        prior = {"records": [{"id": "K01", **review}]}
        selected, _ = selected_reviews(source, "bundle", final, prior, "prior")
        self.assertFalse(selected["K01"]["target_covered"])
        with self.assertRaisesRegex(ValueError, "bundle SHA256"):
            selected_reviews(source, "changed", final, prior, "prior")
        with self.assertRaisesRegex(ValueError, "semantic review SHA256"):
            selected_reviews(source, "bundle", final, prior, "changed")
        prior["records"][0]["core_valid"] = False
        with self.assertRaisesRegex(ValueError, "frozen semantic judgement"):
            selected_reviews(source, "bundle", final, prior, "prior")

    def test_quote_offsets_are_source_derived_and_ambiguous_quotes_rejected(self):
        pk = str(uuid.uuid4())
        passages = {pk: {"text": "Drug A. Drug A."}}
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            exact_quote({"passage": pk.replace("-", ""), "quote": "Drug A"}, passages)
        quote = exact_quote({"passage": pk, "quote": "Drug A", "start": 8, "end": 14}, passages)
        self.assertEqual(quote["start"], 8)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            exact_quote({"passage": pk, "quote": "Drug B", "start": 0, "end": 6}, passages)

    def test_valid_extra_fact_is_not_failed_for_missing_requested_target(self):
        passage_id = str(uuid.uuid4())
        candidate = SimpleNamespace(pk=uuid.uuid4(), group_id="K01", fact={"treatment": "Drug A"})
        row = {"core_valid": True, "target_covered": False, "participants_usable": True,
               "source_quotes": [{"passage": passage_id, "quote": "Drug A."}],
               "review_basis": "Fixture participant review", "diagnostics": ["Target not covered"]}
        prior = {"rationale": "Fixture relation supported", "source_quotes": row["source_quotes"]}
        def packet(c, extras):
            return {"candidate": {"id": str(c.pk), "fact": c.fact}, "extra_passage_ids": extras,
                    "passages": [{"id": passage_id, "text": "Drug A."}]}
        result = quality_record(candidate, row, prior, build_packet=packet)
        self.assertEqual(result["decision"], "pass")
        self.assertFalse(result["quality_dimensions"]["target_covered"])
        self.assertNotIn("approved_links", result)
        self.assertEqual(len(result["supporting_quotes"]), 1)
