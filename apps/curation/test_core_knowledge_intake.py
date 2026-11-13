import unittest
from copy import deepcopy

from apps.curation.core_knowledge_intake import assemble


class CoreKnowledgeIntakeTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "id": "T01",
            "article": {"pmid": "fixture", "title": "DA and another drug"},
            "passages": [
                {
                    "id": "a",
                    "alias": "P1",
                    "role": "anchor",
                    "text": "Drug-A plus Drug-B improved the result in metastatic breast cancer. Drug-A was studied.",
                },
                {
                    "id": "s",
                    "alias": "P2",
                    "role": "support",
                    "text": "Drug-A plus Drug-B was given. Drug-C was background.",
                },
            ],
            "entities": [
                {
                    "id": "T01.e1",
                    "type": "drug",
                    "text": "Drug-A",
                    "locations": [{"passage_id": "a", "start": 0, "end": 6}],
                    "link": {"candidate_id": "TEST:A", "status": "candidate"},
                },
                {
                    "id": "T01.e2",
                    "type": "drug",
                    "text": "Drug-B",
                    "locations": [{"passage_id": "a", "start": 12, "end": 18}],
                    "link": {"candidate_id": None},
                },
                {
                    "id": "T01.e3",
                    "type": "drug",
                    "text": "Drug-C",
                    "locations": [{"passage_id": "s", "start": 30, "end": 36}],
                    "link": {"candidate_id": "TEST:C"},
                },
                {
                    "id": "T01.e4",
                    "type": "disease",
                    "text": "metastatic breast cancer",
                    "locations": [{"passage_id": "a", "start": 42, "end": 66}],
                },
            ],
        }
        self.output = {
            "status": "classified",
            "payload": {
                "fact": {
                    "relation": "treatment_outcome",
                    "statement": "Drug-A plus Drug-B improved the result.",
                    "drug_names": ["Drug-A", "Drug-B"],
                    "entity_ids": ["T01.e1", "T01.e2", "T01.e4"],
                    "population": "metastatic breast cancer",
                    "treatment": "Drug-A plus Drug-B",
                    "comparator": None,
                    "conditions": [],
                    "results": [],
                    "scope": "current study",
                    "evidence": ["P1", "P2"],
                },
                "reason": None,
            },
        }

    def test_exact_combination_sources_and_existing_candidates_are_preserved(self):
        original_packet, original_output = deepcopy(self.packet), deepcopy(self.output)
        result = assemble(self.packet, self.output)
        self.assertEqual(result["structure_status"], "core_structurally_valid")
        candidate = result["candidate"]
        self.assertEqual(candidate["fact"], self.output["payload"]["fact"])
        self.assertEqual([d["name"] for d in candidate["drugs"]], ["Drug-A", "Drug-B"])
        self.assertEqual(len(candidate["drugs"][0]["locations"]), 3)
        self.assertEqual(
            candidate["drugs"][0]["standard_candidates"][0]["candidate_id"], "TEST:A"
        )
        self.assertNotIn("Drug-C", [d["name"] for d in candidate["drugs"]])
        passages = {p["id"]: p["text"] for p in self.packet["passages"]}
        for drug in candidate["drugs"]:
            for pos in drug["locations"]:
                self.assertEqual(
                    passages[pos["passage_id"]][pos["start"] : pos["end"]], drug["name"]
                )
        self.assertEqual(candidate["business_disease_links"][0]["label"], "乳腺癌")
        self.assertEqual(
            candidate["business_disease_links"][0]["source_mentions"][0][
                "original_text"
            ],
            "metastatic breast cancer",
        )
        self.assertEqual(self.packet, original_packet)
        self.assertEqual(self.output, original_output)
        self.assertEqual(
            candidate["semantic_verification"], "not_established_by_script"
        )

    def test_unknown_shortened_or_renamed_drug_is_not_repaired(self):
        self.packet["passages"][1]["text"] += " nivolumab was mentioned separately."
        for name in ["DA", "drug-a", "Drug A", "Unknown", "nivo"]:
            with self.subTest(name=name):
                output = deepcopy(self.output)
                output["payload"]["fact"]["drug_names"] = [name]
                result = assemble(self.packet, output)
                self.assertEqual(result["structure_status"], "invalid_structure")
                self.assertIn(
                    "drug_name_not_literal", {e["code"] for e in result["issues"]}
                )
                self.assertEqual(result["candidate"]["drugs"][0]["name"], name)

    def test_business_group_uses_only_selected_approved_disease_mentions(self):
        output = deepcopy(self.output)
        output["payload"]["fact"]["entity_ids"] = ["T01.e1", "T01.e2"]
        output["payload"]["fact"]["population"] = None
        self.assertEqual(
            assemble(self.packet, output)["candidate"]["business_disease_links"], []
        )
        self.packet["entities"][3]["text"] = "prostate cancer"
        self.assertEqual(
            assemble(self.packet, self.output)["candidate"]["business_disease_links"],
            [],
        )

    def test_empty_legacy_ids_allow_literal_drugs_without_filling_ids(self):
        output = deepcopy(self.output)
        output["payload"]["fact"]["entity_ids"] = []
        result = assemble(self.packet, output)
        self.assertEqual(result["version"], "core-knowledge-intake-assembly-v2")
        self.assertEqual(result["structure_status"], "core_structurally_valid")
        self.assertEqual(result["candidate"]["fact"]["entity_ids"], [])
        self.assertEqual(result["candidate"]["entities"], [])
        self.assertIn("empty_entity_ids", {e["code"] for e in result["diagnostics"]})
        mention = result["candidate"]["business_disease_links"][0]["source_mentions"][0]
        self.assertTrue(mention["from_population_context"])
        self.assertEqual(mention["original_text"], "metastatic breast cancer")
        for names in ([], ["Drug-A", "invented drug"]):
            output["payload"]["fact"]["drug_names"] = names
            result = assemble(self.packet, output)
            self.assertEqual(result["structure_status"], "invalid_structure")
            self.assertIn("empty_entity_ids", {e["code"] for e in result["issues"]})

    def test_off_anchor_is_diagnostic_but_missing_or_invalid_evidence_fails(self):
        output = deepcopy(self.output)
        output["payload"]["fact"]["evidence"] = ["P2"]
        result = assemble(self.packet, output)
        self.assertEqual(result["structure_status"], "core_structurally_valid")
        self.assertFalse(result["anchor_cited"])
        self.assertEqual(result["diagnostics"][0]["code"], "anchor_not_cited")
        self.assertEqual([x["alias"] for x in result["candidate"]["evidence"]], ["P2"])
        for citations, code in [
            ([], "no_actual_evidence"),
            (["P404"], "unknown_passage"),
        ]:
            output["payload"]["fact"]["evidence"] = citations
            result = assemble(self.packet, output)
            self.assertEqual(result["structure_status"], "invalid_structure")
            self.assertIn(code, {e["code"] for e in result["issues"]})

    def test_format_adaptation_changes_shapes_only(self):
        output = deepcopy(self.output)
        output["payload"].pop("reason")
        output["payload"]["fact"]["conditions"] = "original condition"
        original = deepcopy(output)
        result = assemble(self.packet, output)
        self.assertEqual(result["structure_status"], "core_structurally_valid")
        self.assertEqual(len(result["format_changes"]), 2)
        self.assertEqual(
            result["candidate"]["fact"]["conditions"], ["original condition"]
        )
        self.assertEqual(
            result["candidate"]["fact"]["drug_names"], ["Drug-A", "Drug-B"]
        )
        self.assertEqual(output, original)

    def test_unknown_entity_and_unlocated_entity_still_fail(self):
        for entity_ids, code in [
            (["missing"], "unknown_entity_id"),
            (["T01.e1"], "unlocated_upstream_entity"),
        ]:
            packet, output = deepcopy(self.packet), deepcopy(self.output)
            packet["entities"][0]["locations"] = []
            output["payload"]["fact"]["entity_ids"] = entity_ids
            result = assemble(packet, output)
            self.assertEqual(result["structure_status"], "invalid_structure")
            self.assertIn(code, {e["code"] for e in result["issues"]})

    def test_missing_names_fail_but_empty_non_drug_list_and_abstention_are_allowed(
        self,
    ):
        output = deepcopy(self.output)
        output["payload"]["fact"].pop("drug_names")
        self.assertEqual(
            assemble(self.packet, output)["structure_status"], "invalid_structure"
        )
        output["payload"]["fact"]["drug_names"] = []
        self.assertEqual(assemble(self.packet, output)["candidate"]["drugs"], [])
        abstained = {
            "status": "classified",
            "payload": {"fact": None, "reason": "No grounded finding"},
        }
        self.assertEqual(
            assemble(self.packet, abstained)["structure_status"], "abstained"
        )
        failed = {"status": "output_truncated", "payload": None}
        self.assertEqual(
            assemble(self.packet, failed)["structure_status"], "invalid_structure"
        )


if __name__ == "__main__":
    unittest.main()
