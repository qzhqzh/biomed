import copy
import unittest

from apps.curation.astra_reference_integrity import validate


class AstraReferenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.source = {"articles": [{"pmid": "1", "passages": [{"id": "p", "locator": "body/p:1", "text": "A source sentence."}]}]}
        self.review = {"reviewer": "gpt-6-astra", "reference_type": "source_only_model_reference_not_expert_gold",
                       "records": [{"pmid": "1", "facts": [{"evidence": [{"passage_id": "p", "locator": "body/p:1", "quote": "source sentence"}]}], "uncertainties": []}]}

    def test_verifies_exact_quote_without_claiming_semantic_accuracy(self):
        result = validate(self.source, self.review, expected_pmids={"1"})
        self.assertEqual(result["source_quotes_verified"], 1)
        self.assertFalse(result["semantic_correctness_proved"])

    def test_wrong_quote_or_wrong_article_fails_closed(self):
        review = copy.deepcopy(self.review)
        review["records"][0]["facts"][0]["evidence"][0]["quote"] = "invented"
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate(self.source, review, expected_pmids={"1"})
        review = copy.deepcopy(self.review)
        review["records"][0]["pmid"] = "2"
        with self.assertRaisesRegex(ValueError, "differ"):
            validate(self.source, review, expected_pmids={"1"})
