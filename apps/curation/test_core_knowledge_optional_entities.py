import unittest
from copy import deepcopy

from apps.curation.core_knowledge_optional_entities import defer_optional_entities


class CoreKnowledgeOptionalEntitiesTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "passages": [{"id": "p1", "text": "ipilimumab was given."}],
            "entities": [
                {
                    "id": "e1",
                    "text": "ipilimumab",
                    "type": "drug",
                    "aliases": [],
                    "locations": [{"passage_id": "p1", "start": 0, "end": 10}],
                },
                {
                    "id": "e2",
                    "text": "TREM2+ TAMs",
                    "type": "biomarker",
                    "aliases": ["tumour-associated macrophages"],
                    "locations": [],
                },
            ],
        }
        self.result = {
            "status": "classified",
            "payload": {
                "fact": {
                    "statement": "ipilimumab was given.",
                    "entity_ids": ["e1", "e2"],
                    "drug_names": ["ipilimumab"],
                    "conditions": [],
                    "results": [],
                },
                "reason": None,
            },
        }

    def test_extra_reference_is_preserved_separately_without_changing_raw(self):
        original = deepcopy((self.packet, self.result))
        output = defer_optional_entities(self.packet, self.result)
        self.assertEqual(output["kept_blockers"], [])
        self.assertEqual(output["result_view"]["payload"]["fact"]["entity_ids"], ["e1"])
        unresolved = output["unresolved_entity_refs"][0]
        self.assertEqual(unresolved["entity"], self.packet["entities"][1])
        self.assertEqual(unresolved["reason"], "unlocated_additional_reference")
        self.assertEqual(output["changes"][0]["core_source_entity_ids"], ["e1"])
        expected = deepcopy(self.result)
        expected["payload"]["fact"]["entity_ids"] = ["e1"]
        self.assertEqual(output["result_view"], expected)
        self.assertEqual((self.packet, self.result), original)

    def test_unknown_id_is_not_deferred_or_filled(self):
        self.result["payload"]["fact"]["entity_ids"] = ["e1", "unknown"]
        output = defer_optional_entities(self.packet, self.result)
        self.assertEqual(output["result_view"], self.result)
        self.assertEqual(
            output["kept_blockers"],
            [{"entity_id": "unknown", "reason": "unknown_entity_id"}],
        )

    def test_unlocated_core_types_remain_blockers(self):
        for kind in ["drug", "gene", "protein", "disease", "variant", "treatment"]:
            with self.subTest(kind=kind):
                self.packet["entities"][1]["type"] = kind
                output = defer_optional_entities(self.packet, self.result)
                self.assertEqual(output["result_view"], self.result)
                self.assertEqual(len(output["kept_blockers"]), 1)

    def test_original_or_alias_in_any_nested_fact_text_remains_blocking(self):
        for field, value in [
            ("statement", "TREM2+ TAMs were observed"),
            ("results", [{"group": "TUMOUR-ASSOCIATED MACROPHAGES"}]),
            ("drug_names", ["ipilimumab", "TREM2+ TAMs"]),
        ]:
            with self.subTest(field=field):
                result = deepcopy(self.result)
                result["payload"]["fact"][field] = value
                output = defer_optional_entities(self.packet, result)
                self.assertEqual(output["result_view"], result)
                self.assertEqual(
                    output["kept_blockers"][0]["reason"],
                    "unlocated_reference_in_fact_text",
                )

    def test_forged_location_cannot_supply_core_support(self):
        self.packet["entities"][0]["locations"][0]["end"] = 9
        output = defer_optional_entities(self.packet, self.result)
        self.assertEqual(output["result_view"], self.result)
        self.assertEqual(
            {b["reason"] for b in output["kept_blockers"]},
            {
                "unlocated_core_or_unapproved_entity_type",
                "no_located_core_entity_support",
            },
        )

    def test_existing_source_text_or_bad_optional_span_is_not_discarded(self):
        self.packet["passages"][0]["text"] += " TREM2+ TAMs were observed."
        output = defer_optional_entities(self.packet, self.result)
        self.assertEqual(output["result_view"], self.result)
        self.assertEqual(
            output["kept_blockers"][0]["reason"], "source_mention_requires_location"
        )
        self.packet["entities"][1]["locations"] = [
            {"passage_id": "p1", "start": 0, "end": 10}
        ]
        output = defer_optional_entities(self.packet, self.result)
        self.assertEqual(output["result_view"], self.result)
        self.assertEqual(
            output["kept_blockers"][0]["reason"], "invalid_source_locations"
        )


if __name__ == "__main__":
    unittest.main()
