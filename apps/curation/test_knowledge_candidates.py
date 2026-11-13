import copy
import unittest

from apps.curation.decision import digest
from apps.curation.knowledge_candidates import validate_candidate


class KnowledgeCandidateValidationTests(unittest.TestCase):
    def setUp(self):
        self.article = {
            "pmid": "123",
            "pmcid": "PMC123",
            "doi": "10.example/123",
            "title": "Trial",
            "material_id": "material1",
            "version_id": "version1",
            "source_sha256": "a" * 64,
            "source_url": "https://example.org/article",
            "rights": "test source",
            "acquired_at": "2026-10-23",
        }
        self.packet = {
            "id": "K01",
            "article_pmid": "123",
            "article": self.article,
            "anchor_id": "p1",
            "prompt": "frozen test input",
            "passages": [],
        }
        for passage_id, text, role in [
            ("p1", "Drug A improved survival. Drug A improved survival.", "anchor"),
            ("p2", "Patients with lung cancer received Drug A.", "support"),
        ]:
            self.packet["passages"].append(
                {
                    "id": passage_id,
                    "text": text,
                    "role": role,
                    "pmid": "123",
                    "version_id": "version1",
                    "source_sha256": "a" * 64,
                    "parser_version": "parse-v1",
                    "locator": f"body/{passage_id}",
                    "ordinal": len(self.packet["passages"]),
                    "input_sha256": "b" * 64,
                    "upstream_decision_sha256": "c" * 64,
                }
            )
        self.freeze()
        self.payload = {
            "reason": None,
            "candidate": {
                "statement": "Drug A improved survival in lung cancer.",
                "entities": [
                    {"id": "e1", "type": "drug", "text": "Drug A", "passage_id": "p1"},
                    {
                        "id": "e2",
                        "type": "disease",
                        "text": "lung cancer",
                        "passage_id": "p2",
                    },
                ],
                "disease_ids": ["e2"],
                "intervention_ids": ["e1"],
                "comparator_ids": [],
                "molecular_ids": [],
                "molecular_logic": "none",
                "population": None,
                "regimen": None,
                "comparison": None,
                "outcome": "Improved survival",
                "direction": "benefit",
                "study_scope": "unclear",
                "source_role": "unclear",
                "conditions": [],
                "limitations": [],
                "evidence": [
                    {
                        "passage_id": "p1",
                        "quote": "Drug A improved survival.",
                        "fields": [
                            "statement",
                            "outcome",
                            "intervention_ids",
                            "direction",
                        ],
                    },
                    {
                        "passage_id": "p2",
                        "quote": "lung cancer",
                        "fields": ["disease_ids"],
                    },
                ],
            },
        }

    def freeze(self):
        self.packet["input_sha256"] = digest(
            {k: v for k, v in self.packet.items() if k != "input_sha256"}
        )

    def codes(self, payload=None):
        result = validate_candidate(
            self.packet, self.payload if payload is None else payload
        )
        self.assertFalse(result["valid"])
        return {error["code"] for error in result["errors"]}

    def test_valid_multi_paragraph_provenance_and_all_ambiguous_occurrences(self):
        before = copy.deepcopy((self.packet, self.payload))
        result = validate_candidate(self.packet, self.payload)
        self.assertEqual(result["status"], "structurally_valid_candidate")
        self.assertEqual(result["semantic_status"], "not_assessed")
        self.assertEqual(result["article_provenance"]["pmid"], "123")
        self.assertEqual(result["field_supports"]["disease_ids"], [1])
        evidence = result["evidence_spans"][0]
        self.assertTrue(evidence["ambiguous"])
        self.assertEqual(len(evidence["matches"]), 2)
        self.assertEqual(
            evidence["coordinate_space"], "parsed_passage_unicode_codepoints"
        )
        for position in evidence["matches"]:
            self.assertEqual(
                self.packet["passages"][0]["text"][position["start"] : position["end"]],
                evidence["quote"],
            )
        self.assertEqual((self.packet, self.payload), before)

    def test_unselected_passage_and_altered_quote_are_rejected(self):
        self.payload["candidate"]["evidence"][0]["passage_id"] = "unselected-p"
        self.assertIn("unselected_passage", self.codes())
        self.payload["candidate"]["evidence"][0].update(
            passage_id="p1", quote="Drug A ... improved survival."
        )
        self.assertIn("quote_not_found", self.codes())

    def test_entity_exact_substring_is_required(self):
        self.payload["candidate"]["entities"][0]["text"] = "Drug A therapy"
        self.assertIn("quote_not_found", self.codes())

    def test_dangling_ids_duplicate_ids_and_incompatible_reference_types(self):
        candidate = self.payload["candidate"]
        candidate["intervention_ids"] = ["e99"]
        self.assertIn("dangling_entity_id", self.codes())
        candidate["intervention_ids"] = ["e2"]
        self.assertIn("reference_entity_type", self.codes())
        candidate["entities"][1]["id"] = "e1"
        self.assertIn("duplicate_entity_id", self.codes())

    def test_cross_article_and_source_version_tampering_even_with_new_digest(self):
        for field, value in [
            ("pmid", "456"),
            ("version_id", "other-version"),
            ("source_sha256", "d" * 64),
        ]:
            with self.subTest(field=field):
                original = self.packet["passages"][0][field]
                self.packet["passages"][0][field] = value
                self.freeze()
                self.assertIn("passage_article_mismatch", self.codes())
                self.packet["passages"][0][field] = original
                self.freeze()

    def test_frozen_packet_digest_detects_changed_text(self):
        self.packet["passages"][0]["text"] += " More text."
        self.assertIn("packet_hash_mismatch", self.codes())

    def test_exact_field_tags_and_complete_field_coverage(self):
        candidate = self.payload["candidate"]
        candidate["evidence"][0]["fields"] = ["all", "entities"]
        self.assertIn("invalid_evidence_fields", self.codes())
        candidate["evidence"][0]["fields"] = [
            "statement",
            "outcome",
            "intervention_ids",
        ]
        self.assertIn("missing_field_support", self.codes())

    def test_support_quote_cannot_replace_anchor_result(self):
        self.packet["passages"][1]["text"] += " Drug A improved survival."
        self.freeze()
        self.payload["candidate"]["evidence"][0]["passage_id"] = "p2"
        self.assertIn("missing_anchor_support", self.codes())

    def test_strict_keys_types_enums_and_limits(self):
        for field, value, code in [
            ("direction", {}, "invalid_enum"),
            ("population", False, "nullable_string_required"),
            ("conditions", "none", "string_array_required"),
            ("disease_ids", [True], "string_array_required"),
            ("entities", [None] * 15, "entities_array"),
            ("evidence", [], "evidence_array"),
        ]:
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["candidate"][field] = value
                self.assertIn(code, self.codes(payload))
        self.payload["candidate"]["publication_status"] = "published"
        self.assertIn("object_keys", self.codes())

    def test_duplicate_tags_references_and_entity_type_objects_do_not_crash(self):
        self.payload["candidate"]["disease_ids"] *= 2
        self.payload["candidate"]["evidence"][0]["fields"] *= 2
        self.assertIn("duplicate_values", self.codes())
        self.payload["candidate"]["entities"][0]["type"] = {"invented": True}
        self.assertIn("invalid_entity_type", self.codes())

    def test_null_candidate_has_explicit_abstention_not_positive_validation(self):
        result = validate_candidate(
            self.packet,
            {"candidate": None, "reason": "No therapeutic result in anchor."},
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["entity_spans"], [])
        self.assertIn(
            "abstention_reason_required",
            self.codes({"candidate": None, "reason": None}),
        )

    def test_non_objects_and_missing_fields_do_not_crash(self):
        self.assertIn("object_required", self.codes([]))
        self.assertIn("object_keys", self.codes({}))
        self.assertIn("object_keys", self.codes({"candidate": {}, "reason": None}))


if __name__ == "__main__":
    unittest.main()
