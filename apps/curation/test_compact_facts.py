import copy
import unittest

from apps.curation.compact_facts import apply_review, assemble, build_units
from apps.curation.decision import digest


class CompactFactTests(unittest.TestCase):
    def setUp(self):
        article = {
            "pmid": "123",
            "pmcid": "PMC123",
            "doi": "10.example/123",
            "title": "Trial",
            "material_id": "material1",
            "version_id": "version1",
            "source_sha256": "a" * 64,
            "source_url": "https://example.org/article",
            "rights": "test source",
            "acquired_at": "2026-10-24",
        }
        self.packet = {
            "id": "K01",
            "article_pmid": "123",
            "article": article,
            "anchor_id": "p1",
            "prompt": "frozen original prompt",
            "passages": [],
        }
        for index, (text, role) in enumerate(
            [
                (
                    "Drug A improved survival.  Benefit was significant!\nNo cure was established.",
                    "anchor",
                ),
                ("Patients with lung cancer received Drug A.", "support"),
            ],
            1,
        ):
            self.packet["passages"].append(
                {
                    "id": f"p{index}",
                    "text": text,
                    "role": role,
                    "pmid": "123",
                    "version_id": "version1",
                    "source_sha256": "a" * 64,
                    "parser_version": "parse-v1",
                    "locator": f"body/p{index}",
                    "ordinal": index,
                    "input_sha256": "b" * 64,
                    "upstream_decision_sha256": "c" * 64,
                }
            )
        self.freeze()
        self.compact = {
            "reason": None,
            "fact": {
                "claim": ["Drug A improved survival in lung cancer.", ["K01.u1"]],
                "entities": [
                    ["drug", "Drug A", "K01.u1", ["intervention"]],
                    ["disease", "lung cancer", "K01.u4", ["disease"]],
                ],
                "population": [None, []],
                "regimen": [None, []],
                "comparison": [None, []],
                "outcome": ["Improved survival", ["K01.u1"]],
                "molecular_logic": ["none", []],
                "direction": ["benefit", ["K01.u1"]],
                "study_scope": ["unclear", []],
                "source_role": ["unclear", []],
                "conditions": [],
                "limitations": [],
            },
        }

    def freeze(self):
        self.packet["input_sha256"] = digest(
            {k: v for k, v in self.packet.items() if k != "input_sha256"}
        )

    def assemble(self, compact=None, units=None):
        return assemble(
            self.packet,
            build_units(self.packet) if units is None else units,
            self.compact if compact is None else compact,
        )

    def assert_error(self, result, code):
        self.assertFalse(result["valid"])
        self.assertIn(code, {error["code"] for error in result["errors"]})

    def review(self, decision="accept", **changes):
        return {
            "decision": decision,
            "checks": {
                "groups": "pass",
                "conditions": "pass",
                "numbers": "not_applicable",
            },
            "reason": None,
            "patch": None,
            **changes,
        }

    def test_lossless_units_unicode_and_packet_not_modified(self):
        self.packet["passages"][0]["text"] = " α improved!\n\nβ?  中文。  Last.  "
        self.freeze()
        before = copy.deepcopy(self.packet)
        units = build_units(self.packet)
        self.assertEqual(build_units(self.packet), units)
        for passage in self.packet["passages"]:
            selected = [unit for unit in units if unit["passage_id"] == passage["id"]]
            self.assertEqual(
                "".join(unit["text"] for unit in selected), passage["text"]
            )
            for unit in selected:
                self.assertEqual(
                    passage["text"][unit["start"] : unit["end"]], unit["text"]
                )
        self.assertEqual(self.packet, before)

    def test_exact_assembly_reference_binding_and_no_mutation(self):
        before = copy.deepcopy((self.packet, self.compact))
        result = self.assemble()
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["validation"]["semantic_status"], "not_assessed")
        candidate = result["payload"]["candidate"]
        self.assertEqual(candidate["disease_ids"], ["e2"])
        self.assertEqual(candidate["intervention_ids"], ["e1"])
        self.assertEqual(
            candidate["evidence"][0]["quote"], "Drug A improved survival.  "
        )
        self.assertEqual(
            candidate["evidence"][0]["fields"],
            ["direction", "intervention_ids", "outcome", "statement"],
        )
        entity_span = result["unit_spans"]["entities"][0]
        self.assertEqual(entity_span["matches"], [{"start": 0, "end": 6}])
        self.assertEqual((self.packet, self.compact), before)

    def test_forged_cross_packet_missing_and_boolean_units_rejected(self):
        units = build_units(self.packet)
        for change in (
            {"text": "Invented text"},
            {"passage_id": "other"},
            {"start": False},
            {"role": "support"},
        ):
            with self.subTest(change=change):
                changed = copy.deepcopy(units)
                changed[0].update(change)
                self.assert_error(self.assemble(units=changed), "unit_integrity")
        self.assert_error(self.assemble(units=units[:-1]), "unit_integrity")
        other = copy.deepcopy(self.packet)
        other["id"] = "K02"
        other["input_sha256"] = digest(
            {k: v for k, v in other.items() if k != "input_sha256"}
        )
        self.assert_error(self.assemble(units=build_units(other)), "unit_integrity")
        self.compact["fact"]["claim"][1] = ["K02.u1"]
        self.assert_error(self.assemble(), "unselected_unit")

    def test_packet_tamper_rejected_even_if_units_recomputed(self):
        self.packet["passages"][0]["text"] += " Tampered."
        self.assert_error(assemble(self.packet, [], self.compact), "packet_integrity")
        self.freeze()
        self.packet["passages"][0]["pmid"] = "456"
        self.freeze()
        self.assert_error(assemble(self.packet, [], self.compact), "packet_integrity")

    def test_entity_must_occur_in_exact_selected_unit_not_elsewhere(self):
        self.compact["fact"]["entities"][0][2] = "K01.u2"
        self.assert_error(self.assemble(), "entity_not_in_unit")
        self.compact["fact"]["entities"][0][2] = "K01.u1"
        self.compact["fact"]["entities"][0][1] = "drug a"
        self.assert_error(self.assemble(), "entity_not_in_unit")

    def test_adjacent_selected_units_merge_and_retain_field_bindings(self):
        self.compact["fact"]["conditions"] = [["Significant benefit", ["K01.u2"]]]
        result = self.assemble()
        self.assertTrue(result["valid"], result["errors"])
        quote = result["payload"]["candidate"]["evidence"][0]
        self.assertEqual(
            quote["quote"], "Drug A improved survival.  Benefit was significant!\n"
        )
        details = result["unit_spans"]["evidence"][0]["units"]
        self.assertEqual(details[1]["unit_id"], "K01.u2")
        self.assertEqual(details[1]["fields"], ["conditions"])
        self.assertNotIn("conditions", details[0]["fields"])

    def test_nonadjacent_selected_units_do_not_include_unselected_text(self):
        self.compact["fact"]["limitations"] = [["No established cure", ["K01.u3"]]]
        result = self.assemble()
        self.assertTrue(result["valid"], result["errors"])
        quotes = result["payload"]["candidate"]["evidence"]
        self.assertEqual(len(quotes), 3)
        self.assertNotIn(
            "Benefit was significant!", "".join(item["quote"] for item in quotes)
        )

    def test_more_than_six_disjoint_quotes_are_retained_and_fail_old_validator(self):
        self.packet["passages"][0]["text"] = " ".join(
            f"Sentence {index}." for index in range(13)
        )
        self.freeze()
        fact = self.compact["fact"]
        fact["entities"] = []
        fact["conditions"] = [
            [f"Condition {index}", [f"K01.u{index}"]] for index in range(1, 14, 2)
        ]
        result = self.assemble()
        self.assert_error(result, "evidence_array")
        self.assertEqual(len(result["payload"]["candidate"]["evidence"]), 7)

    def test_old_anchor_and_type_constraints_still_apply(self):
        self.compact["fact"]["claim"][1] = ["K01.u4"]
        self.assert_error(self.assemble(), "missing_anchor_support")
        self.compact["fact"]["claim"][1] = ["K01.u1"]
        self.compact["fact"]["entities"][1][3] = ["intervention"]
        self.assert_error(self.assemble(), "reference_entity_type")

    def test_shape_errors_are_not_repaired(self):
        for key, value, code in [
            ("claim", {"value": "Claim"}, "support_pair_required"),
            ("population", [None, ["K01.u4"]], "null_with_support"),
            ("direction", [False, []], "invalid_enum"),
            ("conditions", ["A condition"], "support_pair_required"),
            (
                "entities",
                [["outcome", "survival", "K01.u1", ["context"]]],
                "invalid_entity_type",
            ),
        ]:
            with self.subTest(key=key):
                compact = copy.deepcopy(self.compact)
                compact["fact"][key] = value
                result = self.assemble(compact)
                self.assert_error(result, code)
                self.assertIsNone(result["payload"])

    def test_review_accept_rejects_unresolved_checks_or_hidden_patch(self):
        self.assertTrue(apply_review(self.compact, self.review())["valid"])
        for check in ("fail", "uncertain"):
            review = self.review(
                checks={"groups": check, "conditions": "pass", "numbers": "pass"}
            )
            self.assert_error(apply_review(self.compact, review), "unresolved_accept")
        self.assert_error(
            apply_review(self.compact, self.review(patch={})), "accept_with_patch"
        )
        self.assert_error(
            apply_review(self.compact, {**self.review(), "approved": True}),
            "object_keys",
        )

    def test_review_patch_whitelist_copy_and_caller_revalidation(self):
        before = copy.deepcopy(self.compact)
        patch = {"population": ["Patients with lung cancer", ["K01.u4"]]}
        reviewed = apply_review(
            self.compact, self.review("revise", reason="Add population", patch=patch)
        )
        self.assertTrue(reviewed["valid"], reviewed["errors"])
        self.assertTrue(self.assemble(reviewed["compact"])["valid"])
        self.assertEqual(self.compact, before)
        reviewed["compact"]["fact"]["population"][0] = "Changed after return"
        self.assertEqual(patch["population"][0], "Patients with lung cancer")
        self.assertEqual(self.compact, before)
        for invalid_patch in (
            {},
            {"evidence": []},
            {"claim.value": "Invented"},
            {"reason": "Changed"},
        ):
            self.assertFalse(
                apply_review(self.compact, self.review("revise", patch=invalid_patch))[
                    "valid"
                ]
            )
        reviewed = apply_review(
            self.compact,
            self.review("revise", patch={"claim": ["Unknown source", ["K02.u1"]]}),
        )
        self.assertTrue(reviewed["valid"])
        self.assert_error(self.assemble(reviewed["compact"]), "unselected_unit")

    def test_abstention_is_valid_review_transaction_but_not_successful_candidate(self):
        reviewed = apply_review(
            self.compact,
            self.review("abstain", reason="Cannot resolve conflicting cohorts"),
        )
        self.assertTrue(reviewed["valid"])
        result = self.assemble(reviewed["compact"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "abstained")
        self.assertIsNone(result["payload"]["candidate"])
        self.assert_error(
            apply_review(reviewed["compact"], self.review()), "accept_abstention"
        )

    def test_explicit_patch_can_repair_bad_value_but_cannot_add_missing_fields(self):
        self.compact["fact"]["entities"][0][0] = "outcome"
        before = copy.deepcopy(self.compact)
        self.assert_error(self.assemble(), "invalid_entity_type")
        self.assert_error(
            apply_review(self.compact, self.review()), "invalid_entity_type"
        )
        entities = copy.deepcopy(self.compact["fact"]["entities"])
        entities[0][0] = "drug"
        review = self.review("revise", patch={"entities": entities})
        result = apply_review(self.compact, review)
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(self.assemble(result["compact"])["valid"])
        self.assertEqual(self.compact, before)
        # An unrelated patch must not hide the original malformed value.
        self.assert_error(
            apply_review(
                self.compact, self.review("revise", patch={"population": [None, []]})
            ),
            "invalid_entity_type",
        )
        del self.compact["fact"]["entities"]
        self.assert_error(apply_review(self.compact, review), "object_keys")


if __name__ == "__main__":
    unittest.main()
