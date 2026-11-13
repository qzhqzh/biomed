import unittest

from scripts.prepare_knowledge_intake import association, whole_mention


class IntakePreparationTests(unittest.TestCase):
    def test_reject_embedded_er_but_keep_real_er(self):
        text = "HER2-positive with ER status"
        self.assertFalse(whole_mention(text, 1, 3))
        start = text.index("ER status")
        self.assertTrue(whole_mention(text, start, start + 2))

    def test_plural_source_span_is_not_rewritten(self):
        text = "CRP responders, CRP nonresponders and ICTs"
        for name in ("CRP responder", "CRP nonresponder", "ICT"):
            start = text.index(name)
            self.assertTrue(whole_mention(text, start, start + len(name)))
            self.assertEqual(text[start:start + len(name)], name)

    def test_context_ici_is_not_durvalumab_relationship(self):
        fact = {"drug_names": ["durvalumab"], "entity_ids": ["K08.e3"]}
        self.assertEqual(association({"id": "K08.e4", "text": "ICI"}, fact), "context_only")
        self.assertEqual(association({"id": "K08.e3", "text": "durvalumab"}, fact), "model_selected")

    def test_literal_fact_association_does_not_claim_model_id(self):
        entity = {"id": "e1", "text": "ipilimumab"}
        self.assertEqual(association(entity, {"entity_ids": [], "drug_names": ["ipilimumab + nivolumab"]}), "exact_fact_text")
        self.assertEqual(association({"id": "e2", "text": "ER"}, {"treatment": "HER2 therapy"}), "context_only")
