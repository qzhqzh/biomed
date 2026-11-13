import unittest
from copy import deepcopy

from apps.curation.core_knowledge_projection import project


class CoreKnowledgeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "passages": [
                {
                    "id": "p1",
                    "role": "anchor",
                    "text": (
                        "TREM2 with ipilimumab plus nivolumab; etoposide–platinum was reported. "
                        "cisplatin was background. ICT was discussed. T-DXd was also mentioned."
                    ),
                }
            ],
            "entities": [
                {"id": "e1", "type": "gene", "text": "TREM2"},
                {
                    "id": "e2",
                    "type": "drug",
                    "text": "ipilimumab",
                    "link": {"candidate_id": "RXNORM:1094833", "status": "candidate"},
                },
                {"id": "e3", "type": "drug", "text": "nivolumab"},
                {"id": "e4", "type": "drug", "text": "etoposide"},
                {
                    "id": "e5",
                    "type": "drug",
                    "text": "platinum",
                    "candidate_guards": {
                        "RXNORM:x": {
                            "reasons": [{"code": "ingredient_class_ambiguity"}]
                        }
                    },
                },
                {"id": "e6", "type": "drug", "text": "cisplatin"},
                {"id": "e7", "type": "treatment", "text": "ICT"},
                {"id": "e8", "type": "drug", "text": "ICT"},
            ],
        }

    def run_names(self, names):
        return project({"fact": {"drug_names": names}}, self.packet)

    def test_selected_gene_is_not_a_drug_and_source_remains(self):
        result = self.run_names(["TREM2", "ipilimumab"])
        self.assertEqual([d["name"] for d in result["drugs"]], ["ipilimumab"])
        other = result["other_source_terms"][0]
        self.assertEqual(other["name"], "TREM2")
        self.assertEqual(other["source_entity_ids"], ["e1"])
        self.assertTrue(other["locations"])

    def test_literal_drug_keeps_candidates_and_does_not_add_background_drugs(self):
        result = self.run_names(["ipilimumab"])
        self.assertEqual(len(result["drugs"]), 1)
        drug = result["drugs"][0]
        self.assertEqual(drug["name"], "ipilimumab")
        self.assertEqual(
            drug["standard_candidates"][0]["candidate_id"], "RXNORM:1094833"
        )
        self.assertTrue(drug["selected_directly"])
        self.assertEqual(drug["derived_from"], [])

    def test_selected_long_source_phrase_reuses_only_literal_known_components(self):
        phrase = "ipilimumab plus nivolumab"
        candidate = {"fact": {"drug_names": [phrase]}}
        original = deepcopy((candidate, self.packet))
        result = project(candidate, self.packet)
        self.assertEqual(
            [d["name"] for d in result["drugs"]], ["ipilimumab", "nivolumab"]
        )
        regimen = result["regimen_mentions"][0]
        self.assertEqual(regimen["name"], phrase)
        self.assertEqual(regimen["components"], ["ipilimumab", "nivolumab"])
        self.assertEqual(regimen["relationship"], "not_inferred")
        for drug in result["drugs"]:
            self.assertEqual(drug["derived_from"], [phrase])
            self.assertFalse(drug["selected_directly"])
            for pos in drug["locations"]:
                self.assertEqual(
                    self.packet["passages"][0]["text"][pos["start"] : pos["end"]],
                    drug["name"],
                )
        self.assertEqual((candidate, self.packet), original)

    def test_class_member_is_not_guessed_from_existing_background_drug(self):
        result = self.run_names(["etoposide–platinum", "platinum"])
        self.assertEqual([d["name"] for d in result["drugs"]], ["etoposide"])
        cls = result["regimen_mentions"][0]["unresolved_components"][0]
        self.assertEqual((cls["name"], cls["kind"]), ("platinum", "drug_class"))
        self.assertEqual(result["unresolved_terms"][0]["status"], "unspecified_member")

    def test_frozen_treatment_type_wins_over_unlinked_drug_type(self):
        result = self.run_names(["ICT"])
        self.assertEqual(result["drugs"], [])
        self.assertEqual(result["regimen_mentions"][0]["name"], "ICT")

    def test_unknown_and_case_or_prefix_rewrites_stay_unresolved(self):
        result = self.run_names(["T-DXd", "Nivolumab", "nivo"])
        self.assertEqual(result["drugs"], [])
        self.assertEqual(
            [t["name"] for t in result["unresolved_terms"]],
            ["T-DXd", "Nivolumab", "nivo"],
        )
        self.assertTrue(result["unresolved_terms"][0]["locations"])
        self.assertEqual(result["unresolved_terms"][1]["locations"], [])
        self.assertEqual(result["unresolved_terms"][2]["locations"], [])

    def test_existing_strong_drug_candidate_can_supply_type_evidence(self):
        self.packet["entities"][1]["type"] = "biomarker"
        self.assertEqual(
            self.run_names(["ipilimumab"])["drugs"][0]["name"], "ipilimumab"
        )


if __name__ == "__main__":
    unittest.main()
