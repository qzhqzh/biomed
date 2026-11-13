import copy
import unittest
import uuid

from apps.curation.entity_normalization import (
    candidates,
    normalize_bundle,
    participants,
)
from apps.curation.light_entity_linking import load_index

VERSION_ID = "44444444-4444-4444-8444-444444444444"
PASSAGE = "55555555-5555-4555-8555-555555555555"
LOCAL = "66666666-6666-4666-8666-666666666666"


def registry():
    return {"schema_version": "biomed.entity-registry.snapshot.v1", "source": {"fixture": True},
            "entities": [{"id": LOCAL, "key": "test:alpha", "kind": "drug", "name": "alpha molecule",
                          "name_en": "alpha molecule", "status": "active", "level": "not_applicable",
                          "external_ids": {}, "provenance": {"entity_level": "drug_ingredient"}}], "rules": []}


def bundle(label, *, version=VERSION_ID, source_run="synthetic-first"):
    quote = f"The study administered {label} and reported the response."
    return {"schema_version": "knowledge-candidates-v1", "source_run": source_run,
            "records": [{"group_id": "K1", "version_id": version, "source_sha256": "a" * 64,
                         "entities": [{"ref": "drug", "kind": "drug", "label": label}],
                         "fact": {"entity_roles": {"drug": ["intervention"]}},
                         "anchors": [{"purpose": "evidence", "passage_id": PASSAGE, "start": 0,
                                      "end": len(quote), "quote": quote}]}]}


def index():
    return load_index({"concepts": [{"release_id": "r", "identifier": str(n), "label": name, "synonyms": []}
                                    for n, name in enumerate(("beta molecule", "gamma molecule"), 1)],
                       "releases": {"r": {"namespace": "RXNORM", "version": "test"}},
                       "genes": [], "gene_version": {}})


def match(item, *, decision="MATCH", candidate="local:" + LOCAL, name=None, kind="drug", reuse_scope="global"):
    return {"decision": decision, "candidate_id": candidate if decision == "MATCH" else None,
            "canonical_name": name, "entity_level": "drug_ingredient", "main_cancer_id": None,
            "normalized_kind": kind, "reuse_scope": reuse_scope,
            "evidence_quote": item["contexts"][0]["quote"], "reason": "Synthetic test decision, not a medical reference"}


class EntityNormalizationTests(unittest.TestCase):
    def test_two_previously_unseen_standard_names_use_the_same_code_path(self):
        for label in ("beta molecule", "gamma molecule"):
            with self.subTest(label=label):
                original = bundle(label)
                before = copy.deepcopy(original)
                patch, updated, report = normalize_bundle(original, registry(), index())
                self.assertEqual(report["counts"], {"resolved": 1})
                self.assertEqual(report["model_decisions"], 0)
                self.assertEqual(len(patch["entities"]), 1)
                self.assertEqual(patch["rules"][0]["scope"]["type"], "global")
                self.assertEqual(original, before)
                later = bundle(label, version=PASSAGE, source_run="unseen-later-article")
                patch, _, replay = normalize_bundle(later, updated, index(), decide=lambda item: self.fail("Known name must not call the model"))
                self.assertEqual(replay["counts"], {"reused": 1})
                self.assertEqual(patch["entities"], [])
                self.assertEqual(patch["rules"], [])

    def test_model_confirmed_full_alias_is_saved_and_reused_by_a_later_article(self):
        original = bundle("another full drug name")
        patch, updated, first = normalize_bundle(original, registry(), index(), decide=match)
        self.assertEqual(first["model_decisions"], 1)
        self.assertEqual(patch["entities"], [])
        self.assertEqual(patch["rules"][0]["origin"], "model")
        later = bundle("another full drug name", version=PASSAGE, source_run="later")
        _, _, replay = normalize_bundle(later, updated, index(), decide=lambda item: self.fail("Do not reinterpret the same confirmed alias"))
        self.assertEqual(replay["counts"], {"reused": 1})

    def test_article_abbreviation_does_not_escape_its_scope(self):
        for label in ("ABC", "la/mUC"):
            patch, updated, _ = normalize_bundle(bundle(label), registry(), index(), decide=match)
            self.assertEqual(patch["rules"][0]["scope"]["type"], "article")
            _, _, later = normalize_bundle(bundle(label, version=PASSAGE), updated, index())
            self.assertEqual(later["counts"], {"waiting_configuration": 1})

    def test_definitions_can_come_from_other_records_of_the_same_article_only(self):
        first = bundle("ABC")
        other = bundle("beta molecule")["records"][0]
        other["group_id"] = "K2"
        quote = "The study defined beta molecule (ABC) as its intervention."
        other["anchors"][0].update(passage_id=LOCAL, quote=quote, end=len(quote))
        first["records"].append(other)
        self.assertIn("(ABC)", participants(first)[0]["contexts"][0]["quote"])
        other["version_id"] = PASSAGE
        self.assertNotIn("(ABC)", participants(first)[0]["contexts"][0]["quote"])

    def test_dictionary_alias_and_context_definition_survive_placeholder_candidates(self):
        standards = load_index({"concepts": [{"release_id": "r", "identifier": "1", "label": "specific agent",
                                              "synonyms": ["XYZ"]}],
                                "releases": {"r": {"namespace": "RXNORM", "version": "test"}}, "genes": [], "gene_version": {}})
        saved = registry()
        for n in range(20):
            saved["entities"].append({**saved["entities"][0], "id": str(uuid.uuid4()), "key": f"empty-{n}",
                                      "name": "nan", "name_en": "-"})
        item = participants(bundle("XYZ"))[0]
        choices, direct = candidates(item, saved, standards)
        self.assertEqual(choices[0]["label"], "specific agent")
        self.assertIsNone(direct)  # A synonym still needs an identity decision.
        self.assertFalse(any(c["label"] == "nan" for c in choices))

    def test_explicit_combination_is_never_reduced_to_one_ingredient(self):
        original = bundle("ABC")
        quote = "The trial used ABC (beta molecule, gamma molecule)."
        original["records"][0]["anchors"][0].update(quote=quote, end=len(quote))
        patch, _, report = normalize_bundle(original, registry(), index(), decide=lambda _: self.fail("Explicit definition needs no model"))
        self.assertEqual(report["counts"], {"type_corrected": 1})
        self.assertEqual(patch["rules"][0]["provenance"]["normalized_kind"], "treatment")
        self.assertEqual(patch["entities"], [])

    def test_explicit_type_output_is_assembled_without_creating_a_fake_drug(self):
        patch, _, report = normalize_bundle(bundle("non-drug treatment"), registry(), index(),
                                            decide=lambda item: match(item, decision="NEW", kind="treatment", name="non-drug treatment"))
        self.assertEqual(report["counts"], {"type_corrected": 1})
        self.assertEqual(patch["entities"], [])
        self.assertTrue(report["records"][0]["assembly_notes"])

    def test_malformed_optional_hierarchy_does_not_crash_or_overwrite_identity(self):
        patch, _, report = normalize_bundle(bundle("another full alias"), registry(), index(),
                                            decide=lambda item: {**match(item), "main_cancer_id": {"invalid": "type"}})
        self.assertEqual(report["counts"], {"resolved": 1})
        self.assertEqual(patch["entities"], [])
        self.assertIsNone(report["records"][0]["decision"]["main_cancer_id"])

    def test_new_source_named_drug_is_registered_without_human_approval(self):
        patch, updated, first = normalize_bundle(bundle("novel named compound"), registry(), index(),
                                                 decide=lambda item: match(item, decision="NEW", name="novel named compound"))
        self.assertEqual(first["counts"], {"resolved": 1})
        self.assertEqual(patch["entities"][0]["status"], "active")
        self.assertEqual(patch["entities"][0]["external_ids"], {})
        self.assertFalse(patch["rules"][0]["provenance"]["human_confirmed"])
        _, _, later = normalize_bundle(bundle("novel named compound", version=PASSAGE), updated, index())
        self.assertEqual(later["counts"], {"reused": 1})

    def test_short_or_article_generic_names_cannot_create_a_global_identity(self):
        for label in ("A1", "compound 7"):
            patch, _, report = normalize_bundle(bundle(label), registry(), index(),
                                                decide=lambda item: match(item, decision="NEW", name=item["label"]))
            self.assertEqual(report["counts"], {"invalid_decision": 1})
            self.assertEqual(patch["entities"], [])

    def test_hallucinated_id_or_name_cannot_create_a_rule(self):
        for decide in (lambda item: match(item, candidate="made-up-target-uuid"),
                       lambda item: match(item, decision="NEW", name="not in this article")):
            patch, _, report = normalize_bundle(bundle("unknown agent"), registry(), index(), decide=decide)
            self.assertEqual(report["counts"], {"invalid_decision": 1})
            self.assertEqual(patch["rules"], [])
            self.assertEqual(patch["entities"], [])

    def test_script_locates_a_real_source_name_instead_of_requiring_model_transcription(self):
        original = bundle("new named agent")

        def decide(item):
            return {**match(item, decision="NEW", name="new named agent"),
                    "evidence_quote": "A shortened model description that is not verbatim"}

        patch, _, report = normalize_bundle(original, registry(), index(), decide=decide)
        self.assertEqual(report["counts"], {"resolved": 1})
        self.assertEqual(report["records"][0]["decision"]["evidence_quote"], original["records"][0]["anchors"][0]["quote"])
        self.assertEqual(patch["entities"][0]["name"], "new named agent")

    def test_one_uncertain_term_does_not_block_other_terms_or_become_a_permanent_rule(self):
        original = bundle("unknown agent")
        second = bundle("beta molecule")["records"][0]
        second["group_id"] = "K2"
        second["anchors"][0]["passage_id"] = LOCAL
        original["records"].append(second)
        patch, _, report = normalize_bundle(original, registry(), index(),
                                            decide=lambda item: match(item, decision="UNRESOLVED"))
        self.assertEqual(report["counts"], {"uncertain": 1, "resolved": 1})
        self.assertEqual(len(patch["rules"]), 1)
        self.assertEqual(patch["rules"][0]["alias"], "beta molecule")

    def test_full_name_with_source_parenthetical_reuses_the_same_new_identity(self):
        original = bundle("new named agent")
        second = bundle("new named agent (NNA)")["records"][0]
        second["group_id"] = "K2"
        second["anchors"][0]["passage_id"] = LOCAL
        original["records"].append(second)
        calls = []

        def decide(item):
            calls.append(item["label"])
            return match(item, decision="NEW", name="new named agent")

        patch, _, report = normalize_bundle(original, registry(), index(), decide=decide)
        self.assertEqual(report["counts"], {"resolved": 2})
        self.assertEqual(calls, ["new named agent"])
        self.assertEqual(len(patch["entities"]), 1)
        self.assertEqual(patch["rules"][1]["scope"]["type"], "article")

    def test_retyping_preserves_the_source_and_does_not_register_a_fake_drug(self):
        original = bundle("a non-drug treatment")
        before = copy.deepcopy(original)
        patch, _, report = normalize_bundle(original, registry(), index(),
                                            decide=lambda item: match(item, decision="RETYPE", kind="treatment"))
        self.assertEqual(report["counts"], {"type_corrected": 1})
        self.assertEqual(patch["entities"], [])
        self.assertEqual(patch["rules"][0]["action"], "ignore")
        self.assertEqual(patch["rules"][0]["provenance"]["normalized_kind"], "treatment")
        self.assertEqual(original, before)

    def test_invalid_source_and_existing_decisions_are_preserved(self):
        original = bundle("beta molecule")
        original["records"][0]["anchors"][0]["end"] += 1
        _, _, report = normalize_bundle(original, registry(), index(), decide=lambda item: self.fail("Bad source cannot call model"))
        self.assertEqual(report["counts"], {"source_missing": 1})
        saved = registry()
        saved["rules"].append({"id": "decision", "kind": "drug", "alias": "beta molecule",
                               "scope": {"type": "global", "key": ""}, "status": "unresolved", "canonical_id": None})
        patch, _, report = normalize_bundle(bundle("beta molecule"), saved, index())
        self.assertEqual(report["counts"], {"existing_conflict": 1})
        self.assertEqual(patch["rules"], [])


if __name__ == "__main__":
    unittest.main()
