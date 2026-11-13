"""Identity and source-scope regressions for the offline mapping preview."""

import copy
import unittest

from scripts.prepare_target_compatibility import build, map_entity, scopes


def record(label, kind="drug", group="K11"):
    ref = f"{group}.e1"
    return {
        "group_id": group,
        "fact": {"entity_roles": {ref: ["intervention" if kind == "drug" else "disease"]},
                 "treatment": label},
        "entities": [{"ref": ref, "label": label, "kind": kind}],
        "anchors": [{"purpose": "evidence", "passage_id": "p1", "quote": label}],
    }


class MappingTests(unittest.TestCase):
    def test_combination_substring_is_only_a_candidate(self):
        r = record("etoposide")
        d = {"drugs": [{"id": "combination", "name": "Etoposide/Olaparib", "aliases": []}]}
        result = map_entity(r, r["entities"][0], d)
        self.assertEqual(result["status"], "unmapped")
        self.assertIsNone(result["target_id"])
        self.assertEqual(result["candidates"][0]["id"], "combination")

    def test_unique_original_name_wins_over_unrelated_synonym_duplicates(self):
        r = record("T-DXd")
        d = {"drugs": [{"id": "original", "name": "T-DXd", "aliases": []},
                       {"id": "synonym1", "name": "Fam-trastuzumab deruxtecan-nxki", "aliases": []},
                       {"id": "synonym2", "name": "Fam-trastuzumab deruxtecan-nxki", "aliases": []}]}
        result = map_entity(r, r["entities"][0], d)
        self.assertEqual(result["target_id"], "original")
        self.assertEqual(result["status"], "exact_target_name")

    def test_real_ambiguous_exact_name_stays_unmapped(self):
        r = record("Drug A")
        d = {"drugs": [{"id": "one", "name": "Drug A", "aliases": []},
                       {"id": "two", "name": "Drug A", "aliases": []}]}
        result = map_entity(r, r["entities"][0], d)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["target_id"])

    def test_acronym_crosswalk_requires_original_full_name(self):
        r = record("DTC", kind="disease")
        d = {"tumors": [{"id": "thyroid", "name": "Thyroid cancer", "aliases": []}]}
        with self.assertRaisesRegex(ValueError, "No original full-name"):
            map_entity(r, r["entities"][0], d)

    def test_cancer_crosswalk_is_a_proposal_and_preserves_fact(self):
        r = record("metastatic breast cancer", kind="disease")
        original = copy.deepcopy(r)
        d = {"tumors": [{"id": "breast", "name": "Breast cancer", "aliases": []}]}
        result = map_entity(r, r["entities"][0], d)
        self.assertEqual(result["target_id"], "breast")
        self.assertIn("本次提议", result["basis"])
        self.assertEqual(r, original)

    def test_apoptosis_readout_is_not_a_mutation_trigger(self):
        r = record("lenvatinib", group="E-42068240-0000")
        r["entities"].append({"ref": "parp", "kind": "protein", "label": "PARP"})
        r["fact"]["entity_roles"]["parp"] = ["molecular"]
        trigger, treatment = scopes(r)
        self.assertEqual(trigger["kind"], "none")
        self.assertEqual(trigger["condition_entity_refs"], [])
        self.assertEqual(trigger["non_trigger_entity_refs"], ["parp"])
        self.assertEqual(treatment["kind"], "alternatives")

    def test_k15_single_drug_keeps_source_alias_and_both_identity_review_rows(self):
        r = record("dordaviprone", group="K15")
        r["entities"].append({"ref": "K15.alias", "kind": "drug", "label": "ONC201"})
        r["fact"]["entity_roles"]["K15.alias"] = ["intervention"]
        r["fact"]["treatment"] = "dordaviprone (ONC201)"
        r["anchors"][0]["quote"] = "The therapy is dordaviprone (ONC201)."
        original = copy.deepcopy(r)
        mappings, notes = build({"source_run": "test", "records": [r]}, {"drugs": [], "tumors": []})
        treatment = mappings["K15"]["treatment_scope"]
        self.assertEqual(treatment["kind"], "single")
        self.assertEqual(treatment["entity_refs"], ["K15.e1"])
        self.assertEqual(treatment["groups"][0]["entity_refs"], ["K15.e1"])
        self.assertIn("ONC201", treatment["reason"])
        self.assertEqual([e["label"] for e in notes[0]["entity_mappings"]], ["dordaviprone", "ONC201"])
        self.assertEqual(r, original)


if __name__ == "__main__":
    unittest.main()
