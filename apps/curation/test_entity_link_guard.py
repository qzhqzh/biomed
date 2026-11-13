import unittest

from apps.curation.entity_link_guard import (
    build_guard_metadata,
    guard_link,
    parse_mondo_scopes,
)


class EntityLinkGuardTests(unittest.TestCase):
    def setUp(self):
        self.obo = '''[Term]
id: TEST:1
name: narrow disease
synonym: "broad disease" RELATED [test:source]
synonym: "ND" EXACT ABBREVIATION [test:source]
synonym: "other label" BROAD []
[Typedef]
id: other
'''
        self.metadata = build_guard_metadata(
            mondo_scopes=parse_mondo_scopes(self.obo.splitlines()),
            rxclasses=[{"id": "CLASS:1", "name": "Example-based Drug", "class_type": "EPC"}],
            sources=[{"version": "test", "sha256": "test-source"}],
        )

    def disease(self, alias):
        return {
            "id": "TEST:1", "synonym_scope": "not_preserved",
            "matching_evidence": [{"dictionary_field": "synonym", "dictionary_text": alias}],
        }

    def test_non_equivalent_scope_is_candidate_only(self):
        for alias in ["broad disease", "other label"]:
            result = guard_link({}, self.disease(alias), {}, self.metadata)
            self.assertEqual(result["decision"], "unresolved")
            self.assertEqual(result["candidate_id"], "TEST:1")
            self.assertEqual(result["reasons"][0]["code"], "non_equivalent_synonym")

    def test_exact_alias_and_independent_label_support_are_retained(self):
        candidate = self.disease("ND")
        self.assertEqual(guard_link({}, candidate, {}, self.metadata)["decision"], "retain")
        candidate = self.disease("broad disease")
        candidate["matching_evidence"].append({"dictionary_field": "label", "dictionary_text": "narrow disease"})
        self.assertEqual(guard_link({}, candidate, {}, self.metadata)["decision"], "retain")

    def test_missing_scope_is_not_invented(self):
        result = guard_link({}, self.disease("not in source"), {}, self.metadata)
        self.assertEqual(result["decision"], "retain")
        self.assertEqual(result["warnings"], ["synonym_scope_unavailable"])

    def test_ingredient_class_collision_requires_local_context(self):
        entity = {"text": "Example"}
        candidate = {"id": "DRUG:1", "entity_level": "drug_ingredient"}
        for text in ["A drug–Example combination was given.", "Example-based therapy", "Example chemotherapy"]:
            packet = {"passages": [{"id": "p1", "text": text}]}
            result = guard_link(entity, candidate, packet, self.metadata)
            self.assertEqual(result["decision"], "unresolved", text)
        for text in [
            "Example ions were quantified. Cancer therapy was discussed.",
            "Example nanoparticles accumulated.",
            "Drug–Example catalyst was measured. Cancer therapy was discussed.",
        ]:
            packet = {"passages": [{"id": "p1", "text": text}]}
            self.assertEqual(guard_link(entity, candidate, packet, self.metadata)["decision"], "retain", text)

    def test_class_metadata_does_not_block_unrelated_ingredients(self):
        packet = {"passages": [{"text": "Drug-A–Drug-B chemotherapy"}]}
        result = guard_link({"text": "Drug-B"}, {"id": "DRUG:2", "entity_level": "drug_ingredient"}, packet, self.metadata)
        self.assertEqual(result["decision"], "retain")

    def test_scope_parser_keeps_repeated_scope_and_filters_ids(self):
        parsed = parse_mondo_scopes(self.obo.splitlines(), identifiers=["absent"])
        self.assertEqual(parsed, {})
        self.assertEqual(self.metadata["synonym_scopes"]["TEST:1"]["nd"], ["EXACT"])
        self.assertNotIn("other", self.metadata["synonym_scopes"])

    def test_guard_does_not_mutate_input_or_claim_verification(self):
        candidate = self.disease("broad disease")
        before = repr(candidate)
        result = guard_link({}, candidate, {}, self.metadata)
        self.assertEqual(repr(candidate), before)
        self.assertNotIn("confirmed_id", result)
        self.assertEqual(result["sources"], self.metadata["sources"])


if __name__ == "__main__":
    unittest.main()
