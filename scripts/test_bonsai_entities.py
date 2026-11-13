import unittest
from copy import deepcopy

from apps.curation.source_entity_types import refine
from scripts.evaluate_bonsai_entities import check_refined_review


class BonsaiEntityReviewTests(unittest.TestCase):
    def setUp(self):
        text = "We studied immune checkpoint inhibition (ICI). ICI therapy was given."
        self.passage = {
            "id": "p1", "pmid": "1", "version_id": "v1", "source_sha256": "s1",
            "parser_version": "fixture", "locator": "body/p:1", "text": text,
        }
        self.packet = {"id": "K01", "article": {"pmid": "1", "version_id": "v1", "source_sha256": "s1"},
                       "passages": [self.passage]}
        start = text.index("ICI")
        self.entity = {
            "id": "K01.e1", "type": "drug", "text": "ICI", "raw_indices": [0],
            "locations": [{"passage_id": "p1", "start": start, "end": start + 3}],
            "link": None,
        }

    def test_actual_type_change_requires_complete_source_review(self):
        typed = refine(self.packet, [self.entity], [self.passage])
        self.assertEqual(len(typed["changes"]), 1)
        review = {"type_rule_checks": [], "core_usable": False,
                  "refined_core_usable": True, "refined_core_errors": []}
        with self.assertRaisesRegex(ValueError, "not completely reviewed"):
            check_refined_review(review, typed, self.packet, [self.passage])

    def test_score_cannot_improve_when_rules_did_nothing(self):
        entity = {**self.entity, "type": "treatment"}
        typed = refine(self.packet, [entity], [self.passage])
        self.assertFalse(typed["changes"])
        review = {"type_rule_checks": [], "core_usable": False,
                  "refined_core_usable": True, "refined_core_errors": []}
        with self.assertRaisesRegex(ValueError, "without changing any type"):
            check_refined_review(review, typed, self.packet, [self.passage])

    def test_source_supported_change_replays_but_mutated_type_is_rejected(self):
        typed = refine(self.packet, [self.entity], [self.passage])
        change = typed["changes"][0]
        check = {k: change[k] for k in ("entity_id", "from_type", "to_type", "rule_id")}
        quote = "immune checkpoint inhibition"
        start = self.passage["text"].index(quote)
        check.update(supported=True, brief="Fixture source definition supports treatment type.",
                     English_source_evidence={"passage_id": "p1", "quote": quote,
                                              "start": start, "end": start + len(quote)})
        review = {"type_rule_checks": [check], "core_usable": False,
                  "refined_core_usable": True, "refined_core_errors": []}
        check_refined_review(review, typed, self.packet, [self.passage])
        changed = deepcopy(typed)
        changed["entities"][0]["type"] = "protein"
        with self.assertRaisesRegex(ValueError, "do not reproduce"):
            check_refined_review(review, changed, self.packet, [self.passage])


if __name__ == "__main__":
    unittest.main()
