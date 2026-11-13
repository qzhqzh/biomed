"""Bounded normalization repair invariants, using synthetic source fixtures."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_entity_registry_operational import (
    build,
    checked_snapshot,
    digest,
    retinoid_dictionary_match,
)


class OperationalRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.state = {"entities": [], "rules": [], "proposed_mentions": []}
        self.items = []
        self.business = {
            "selection": {"model": "bonsai2-27b"},
            "arms": {"bonsai2-27b": {"disease": {"records": []}}},
        }
        self.molecule = {
            "molecule_chembl_id": "CHEMBL705",
            "pref_name": "ALITRETINOIN",
            "molecule_synonyms": [{"molecule_synonym": "9-cis retinoic acid"}],
        }

    def entity(
        self,
        key,
        *,
        kind="drug",
        status="active",
        level="not_applicable",
        external=None,
    ):
        row = {
            "id": key,
            "key": key,
            "kind": kind,
            "name": key,
            "status": status,
            "level": level,
            "revision": 1,
            "external_ids": external or {},
        }
        self.state["entities"].append({**row, "state_sha256": digest(row)})

    def item(self, item_id, label, quote, *, kind="drug", canonical=None):
        self.items.append(
            {
                "id": item_id,
                "label": label,
                "kind": kind,
                "mentions": [
                    {
                        "version_id": self.scope.replace("-", ""),
                        "source_support": [{"quote": quote}],
                    }
                ],
            }
        )
        rule = {
            "id": item_id,
            "kind": kind,
            "alias": label,
            "scope": {"type": "article", "key": self.scope},
            "canonical_id": canonical,
            "status": "proposed",
            "origin": "model",
            "action": "new",
            "revision": 1,
            "evidence": [{"quote": quote}],
            "provenance": {"item_id": item_id},
        }
        self.state["rules"].append({**rule, "state_sha256": digest(rule)})
        self.state["proposed_mentions"].append({"applied_rule_id": item_id})

    def run_builder(self):
        (self.root / "comparison-business-v2.json").write_text(
            json.dumps(self.business)
        )
        (self.root / "items.json").write_text(json.dumps({"records": self.items}))
        chembl = self.root / "chembl.json"
        chembl.write_text(json.dumps([self.molecule]))
        original = copy.deepcopy(self.state)
        with patch(
            "scripts.build_entity_registry_operational.rescore",
            return_value=self.business,
        ):
            result = build(
                self.root,
                self.state,
                chembl,
                expected_references=len(self.state["proposed_mentions"]),
                expected_rules=len(self.state["rules"]),
            )
        self.assertEqual(self.state, original)
        return result

    def test_hash_guard_detects_changed_state(self):
        row = {"alias": "unchanged", "revision": 1}
        snapshot = {**row, "state_sha256": digest(row)}
        self.assertEqual(checked_snapshot(snapshot), row)
        snapshot["alias"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            checked_snapshot(snapshot)

    def test_isomer_alias_requires_source_expansion_and_never_guesses_generic_ra(self):
        source = ["retinoic acid (RA) agents"]
        self.assertEqual(
            retinoid_dictionary_match("9-cis RA", source, [self.molecule])[
                "identifier"
            ],
            "CHEMBL705",
        )
        for label, quotes in [
            ("RA", source),
            ("retinoic acid", source),
            ("all-trans RA", source),
            ("9-cis RA", []),
        ]:
            self.assertIsNone(retinoid_dictionary_match(label, quotes, [self.molecule]))
        self.assertIsNone(
            retinoid_dictionary_match(
                "9-cis RA", source, [self.molecule, self.molecule]
            )
        )

    def test_new_subtype_decision_can_adopt_correct_main_without_new_disease(self):
        main = "seed:prostate"
        self.entity(main, kind="disease", level="main")
        label = "metastatic castration-resistant prostate cancer"
        self.item("E014", label, label + " was studied", kind="disease")
        prediction = {
            "decision": "NEW",
            "candidate_id": None,
            "main_cancer_id": main,
            "subtype_candidate_id": None,
        }
        self.business["arms"]["bonsai2-27b"]["disease"]["records"] = [
            {
                "id": "E014",
                "business_correct": True,
                "direct_main_correct": True,
                "prediction": prediction,
                "extended_fields": {"extension_fields_complete": False},
            }
        ]
        result = self.run_builder()
        rule = result["rule_updates"][0]
        self.assertEqual(rule["canonical_key"], main)
        self.assertEqual(
            rule["provenance"]["required_qualifiers"],
            ["metastatic", "castration-resistant"],
        )
        self.assertEqual(rule["provenance"]["raw_prediction"], prediction)
        self.assertFalse(
            rule["provenance"]["extended_fields"]["extension_fields_complete"]
        )
        self.assertEqual(result["entities"], [])
        self.assertEqual(
            rule["expected_content_sha256"], self.state["rules"][0]["state_sha256"]
        )
        self.business["arms"]["bonsai2-27b"]["disease"]["records"][0][
            "direct_main_correct"
        ] = False
        with self.assertRaisesRegex(ValueError, "explanation-only"):
            self.run_builder()

    def test_liarozole_named_inhibitor_creates_local_drug_without_fake_external_id(
        self,
    ):
        self.item(
            "E035",
            "Liarozole",
            "CYP26A1 inhibitors Liarozole and Talarozole were tested.",
        )
        result = self.run_builder()
        self.assertEqual(result["entities"][0]["kind"], "drug")
        self.assertEqual(result["entities"][0]["external_ids"], {})
        self.assertEqual(result["rule_updates"][0]["action"], "new")
        self.assertEqual(result["rules"][0]["scope"]["type"], "global")
        self.items[0]["mentions"][0]["source_support"][0]["quote"] = "Liarozole"
        with self.assertRaisesRegex(ValueError, "explicit source role"):
            self.run_builder()

    def test_generic_ra_reuses_class_and_keeps_article_scope(self):
        self.entity("existing:ra-class", status="candidate")
        quote = "We tested retinoic acid (RA) agents."
        self.item("E040", "RA", quote)
        self.item("E041", "retinoic acid", quote, canonical="existing:ra-class")
        result = self.run_builder()
        self.assertEqual(
            {r["canonical_key"] for r in result["rule_updates"]}, {"existing:ra-class"}
        )
        self.assertEqual(result["rules"], [])
        self.assertEqual(result["entities"], [])
        self.assertEqual(len(result["new_candidates_activation"]), 1)
        self.assertEqual(
            result["new_candidates_activation"][0]["provenance_patch"],
            {"entity_level": "drug_class"},
        )

    def test_9cis_activates_existing_molecule_identity(self):
        self.entity(
            "existing:alitretinoin",
            status="candidate",
            external={"CHEMBL": "CHEMBL705"},
        )
        self.item("E022", "9-cis RA", "9-cis RA was tested.")
        self.items.append(
            {
                "id": "E040",
                "kind": "drug",
                "label": "RA",
                "mentions": [
                    {
                        "version_id": self.scope,
                        "source_support": [{"quote": "retinoic acid (RA) agents"}],
                    }
                ],
            }
        )
        result = self.run_builder()
        self.assertEqual(
            result["rule_updates"][0]["canonical_key"], "existing:alitretinoin"
        )
        self.assertEqual(
            result["new_candidates_activation"][0]["entity_key"],
            "existing:alitretinoin",
        )
        self.assertNotIn("RA", {r["alias"] for r in result["rules"]})
        self.assertEqual(result["entities"], [])


if __name__ == "__main__":
    unittest.main()
