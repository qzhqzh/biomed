import unittest
from copy import deepcopy

from apps.curation.core_knowledge_name_resolution import resolve
from apps.curation.core_knowledge_projection import project


class CoreKnowledgeNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "passages": [
                {
                    "id": "P1",
                    "role": "support",
                    "text": "Dexamethasone was given. TREM2 was measured.",
                }
            ],
            "entities": [
                {
                    "id": "e1",
                    "type": "drug",
                    "text": "Dexamethasone",
                    "link": {
                        "candidate_id": "RXNORM:3264",
                        "status": "research_candidate_not_confirmed",
                    },
                },
                {"id": "e2", "type": "gene", "text": "TREM2"},
            ],
        }
        self.result = {
            "status": "classified",
            "payload": {
                "fact": {
                    "drug_names": ["dexamethasone"],
                    "entity_ids": [],
                    "statement": "dexamethasone was given.",
                    "treatment": "dexamethasone",
                },
                "reason": None,
            },
        }

    def test_case_only_alignment_preserves_raw_and_all_other_fields(self):
        originals = deepcopy((self.packet, self.result))
        result = resolve(self.packet, self.result)
        self.assertEqual(result["unresolved"], [])
        change = result["changes"][0]
        self.assertEqual(
            (change["model_name"], change["source_name"]),
            ("dexamethasone", "Dexamethasone"),
        )
        self.assertEqual(change["source_ids"], ["e1"])
        self.assertEqual(change["method"], "case_only_source_alignment")
        for loc in change["locations"]:
            self.assertEqual(
                self.packet["passages"][0]["text"][loc["start"] : loc["end"]],
                "Dexamethasone",
            )
        expected = deepcopy(self.result)
        expected["payload"]["fact"]["drug_names"] = ["Dexamethasone"]
        self.assertEqual(result["result_view"], expected)
        self.assertEqual((self.packet, self.result), originals)
        drug = project({"fact": expected["payload"]["fact"]}, self.packet)["drugs"][0]
        self.assertEqual(drug["source_entity_ids"], ["e1"])
        self.assertEqual(
            drug["standard_candidates"][0]["status"], "research_candidate_not_confirmed"
        )

    def test_exact_source_spelling_has_priority(self):
        self.packet["passages"][0]["text"] += " dexamethasone also appears."
        result = resolve(self.packet, self.result)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["result_view"], self.result)

    def test_gene_and_mixed_types_are_not_aligned(self):
        self.result["payload"]["fact"]["drug_names"] = ["trem2"]
        result = resolve(self.packet, self.result)
        self.assertEqual(result["changes"], [])
        self.assertEqual(
            result["unresolved"][0]["reason"], "non_drug_or_mixed_entity_types"
        )
        self.packet["entities"].append({"id": "e3", "type": "drug", "text": "TREM2"})
        self.assertEqual(resolve(self.packet, self.result)["changes"], [])

    def test_alias_prefix_or_unlocated_entity_is_not_expanded(self):
        for name in ["Dex", "dexameth", "unknown"]:
            with self.subTest(name=name):
                self.result["payload"]["fact"]["drug_names"] = [name]
                self.packet["entities"].append(
                    {"id": name, "type": "drug", "text": name.upper()}
                )
                result = resolve(self.packet, self.result)
                self.assertEqual(result["changes"], [])
                self.assertEqual(result["result_view"], self.result)
                self.assertTrue(result["unresolved"])

    def test_same_namespace_identity_conflict_is_not_aligned(self):
        self.packet["entities"].append(
            {
                "id": "e3",
                "type": "drug",
                "text": "Dexamethasone",
                "link": {"candidate_id": "RXNORM:999"},
            }
        )
        result = resolve(self.packet, self.result)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"][0]["reason"], "conflicting_linked_ids")

    def test_different_namespaces_are_not_an_identity_conflict(self):
        self.packet["entities"].append(
            {
                "id": "e3",
                "type": "drug",
                "text": "Dexamethasone",
                "link": {"candidate_id": "CHEMBL:CHEMBL384467"},
            }
        )
        result = resolve(self.packet, self.result)
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["changes"][0]["source_ids"], ["e1", "e3"])

    def test_guarded_class_is_not_a_specific_drug(self):
        self.packet["entities"][0]["candidate_guards"] = {
            "RXNORM:3264": {"reasons": [{"code": "ingredient_class_ambiguity"}]}
        }
        result = resolve(self.packet, self.result)
        self.assertEqual(result["changes"], [])
        self.assertEqual(
            result["unresolved"][0]["reason"], "ambiguous_or_unproven_drug_identity"
        )


if __name__ == "__main__":
    unittest.main()
