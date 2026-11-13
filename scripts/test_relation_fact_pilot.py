import unittest
from copy import deepcopy

from scripts.relation_fact_pilot import assemble_one


class RelationFactAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "id": "T01",
            "anchor_id": "anchor-1",
            "article": {
                "pmid": "fixture-1",
                "version": "frozen-v1",
                "provenance": {"source_sha256": "fixture-source-sha"},
            },
            "passages": [
                {
                    "id": "anchor-1",
                    "alias": "P1",
                    "role": "anchor",
                    "text": "Drug A: median outcome 12 months.\nUncertainty was reported—保留。",
                },
                {
                    "id": "support-1",
                    "alias": "P2",
                    "role": "support",
                    "text": "Previously untreated adults received Drug A in this trial.",
                },
            ],
            "entities": [
                {
                    "id": "T01.e1",
                    "type": "drug",
                    "text": "Drug A",
                    "locations": [{"passage_id": "anchor-1", "start": 0, "end": 6}],
                    "link": {"candidate_id": None},
                },
                {
                    "id": "T01.e2",
                    "type": "treatment",
                    "text": "unlocated paraphrase",
                    "locations": [],
                },
            ],
        }
        self.output = {
            "status": "classified",
            "payload": {
                "fact": {
                    "relation": "treatment_outcome",
                    "statement": "Drug A had a median outcome of 12 months in this trial.",
                    "entity_ids": ["T01.e1"],
                    "population": "Previously untreated adults",
                    "treatment": "Drug A",
                    "comparator": None,
                    "conditions": [],
                    "results": [
                        {
                            "measure": "median outcome",
                            "group": "Drug A",
                            "value": "12 months",
                            "passage": "P1",
                        }
                    ],
                    "scope": "Human current trial",
                    "evidence": ["P1", "P2"],
                },
                "reason": None,
            },
        }

    def assert_rejected(self, output, code):
        result = assemble_one(self.packet, output)
        self.assertEqual(result["status"], "invalid_structure")
        self.assertIn(code, {issue["code"] for issue in result["issues"]})
        return result

    def test_unknown_entity_id_is_rejected_not_fabricated(self):
        self.output["payload"]["fact"]["entity_ids"] = ["T01.missing"]
        result = self.assert_rejected(self.output, "unknown_entity_id")
        if result["candidate"] is not None:
            self.assertEqual(result["candidate"]["entities"], [])

    def test_unlocated_upstream_entity_is_not_a_valid_candidate(self):
        self.output["payload"]["fact"]["entity_ids"] = ["T01.e2"]
        self.assert_rejected(self.output, "unlocated_upstream_entity")

    def test_support_only_citation_is_rejected(self):
        fact = self.output["payload"]["fact"]
        fact["evidence"] = ["P2"]
        fact["results"] = []
        self.assert_rejected(self.output, "anchor_not_cited")

    def test_result_requires_a_known_passage_in_the_evidence(self):
        for alias, expected in (
            ("P999", "unknown_passage"),
            ("P2", "result_passage_not_in_evidence"),
        ):
            with self.subTest(alias=alias):
                output = deepcopy(self.output)
                fact = output["payload"]["fact"]
                fact["evidence"] = ["P1"]
                fact["results"][0]["passage"] = alias
                self.assert_rejected(output, expected)

    def test_unknown_evidence_is_rejected(self):
        self.output["payload"]["fact"]["evidence"].append("P999")
        self.assert_rejected(self.output, "unknown_passage")

    def test_whole_passages_and_article_are_attached_without_source_mutation(self):
        original_packet = deepcopy(self.packet)
        result = assemble_one(self.packet, self.output)
        self.assertEqual(result["status"], "structurally_valid_candidate")
        self.assertEqual(result["issues"], [])
        candidate = result["candidate"]
        self.assertEqual(candidate["article"], original_packet["article"])
        self.assertEqual(candidate["entities"], [original_packet["entities"][0]])
        for passage, citation in zip(
            original_packet["passages"], candidate["evidence"], strict=True
        ):
            self.assertEqual(citation["quote"], passage["text"])
            self.assertEqual(citation["passage_id"], passage["id"])
            self.assertEqual(citation["role"], passage["role"])
            self.assertEqual(citation["start"], 0)
            self.assertEqual(citation["end"], len(passage["text"]))
        self.assertEqual(self.packet, original_packet)

    def test_abstention_requires_a_reason_and_delivers_no_candidate(self):
        result = assemble_one(
            self.packet,
            {"payload": {"fact": None, "reason": "No supported central finding."}},
        )
        self.assertEqual(result["status"], "abstained")
        self.assertIsNone(result["candidate"])
        self.assert_rejected(
            {"payload": {"fact": None, "reason": ""}}, "abstention_reason"
        )

    def test_non_json_runner_failure_does_not_rescue_partial_raw_output(self):
        result = self.assert_rejected(
            {
                "status": "invalid_json",
                "payload": None,
                "raw_output": '{"fact":{"statement":"unfinished',
            },
            "root_shape",
        )
        self.assertIsNone(result["candidate"])

    def test_structural_success_does_not_claim_semantic_verification(self):
        fact = self.output["payload"]["fact"]
        fact["statement"] = "An intentionally unsupported statement for this test."
        result = assemble_one(self.packet, self.output)
        self.assertEqual(result["status"], "structurally_valid_candidate")
        self.assertEqual(result["candidate"]["fact"], fact)
        self.assertEqual(
            result["candidate"]["semantic_verification"], "not_established_by_script"
        )
        self.assertEqual(
            result["candidate"]["state"], "research_candidate_not_published"
        )


if __name__ == "__main__":
    unittest.main()
