import unittest
from copy import deepcopy
from pathlib import Path

from apps.curation.source_entity_types import (
    CLASS_RULE,
    GENE_RULE,
    VARIANT_RULE,
    refine,
)


class SourceEntityTypeTests(unittest.TestCase):
    def passage(self, text, pid="p1", **extra):
        return {"id": pid, "text": text, "pmid": "123", "version_id": "v1",
                "source_sha256": "a" * 64, "parser_version": "p1", "locator": "body/p:1",
                "role": "anchor", **extra}

    def packet_and_entity(self, mention, kind="drug", source=None):
        passage = self.passage(source or f"The patients received {mention} therapy.")
        start = passage["text"].index(mention)
        entity = {"id": "e1", "text": mention, "type": kind,
                  "locations": [{"passage_id": "p1", "start": start, "end": start + len(mention)}],
                  "link": {"candidate_id": None, "proposed_id": None}, "candidate_guards": {"old": {"decision": "retain"}}}
        packet = {"id": "K01", "article": {k: passage[k] for k in ("pmid", "version_id", "source_sha256")},
                  "passages": [passage]}
        return packet, entity

    def test_explicit_class_definition_requires_local_use_and_preserves_source(self):
        packet, entity = self.packet_and_entity("ICI")
        context = [self.passage("We used immune checkpoint inhibition (ICI) and chemotherapy.", "definition")]
        before = deepcopy((packet, entity, context))
        result = refine(packet, [entity], context)
        self.assertEqual(result["entities"][0]["type"], "treatment")
        change = result["changes"][0]
        self.assertEqual(change["rule_id"], CLASS_RULE)
        self.assertEqual(change["standard_link_action"], "unchanged_unlinked")
        self.assertEqual(result["entities"][0]["locations"], entity["locations"])
        self.assertEqual(result["entities"][0]["candidate_guards"], entity["candidate_guards"])
        for evidence in change["evidence"]:
            passage = next(p for p in context + packet["passages"] if p["id"] == evidence["passage_id"])
            self.assertEqual(passage["text"][evidence["start"]:evidence["end"]], evidence["quote"])
            sentence = evidence["sentence"]
            self.assertEqual(passage["text"][sentence["start"]:sentence["end"]], sentence["text"])
        self.assertEqual((packet, entity, context), before)
        self.assertEqual(result["original_entities"], [entity])
        self.assertFalse(result["full_article_context_is_model_input"])

    def test_class_rule_abstains_on_scope_conflict_link_and_inexact_expansion(self):
        packet, entity = self.packet_and_entity("ICI")
        valid = self.passage("immune checkpoint inhibition (ICI)", "definition")
        cases = [
            [self.passage("immune checkpoint inhibition (ICI)", "d", pmid="999")],
            [self.passage("immune checkpoint inhibition (ICI)", "d", version_id="v2")],
            [self.passage("immune checkpoint inhibition (ICI)", "d", source_sha256="b" * 64)],
            [self.passage("immune checkpoint blockade (ICI)", "d")],
            [self.passage("intracellular compound inhibitor (ICI)", "d")],
            [valid, self.passage("intracellular compound inhibitor (ICI)", "d2")],
        ]
        for context in cases:
            with self.subTest(context=context):
                self.assertEqual(refine(packet, [entity], context)["entities"], [entity])
        for selected in ("RXNORM:1", "CHEMBL:1"):
            linked = deepcopy(entity)
            linked["link"]["candidate_id"] = selected
            self.assertEqual(refine(packet, [linked], [valid])["entities"], [linked])
        packet, entity = self.packet_and_entity("ICI", source="The ICI level was measured.")
        self.assertEqual(refine(packet, [entity], [valid])["entities"], [entity])

    def test_variant_rule_uses_complete_explicit_mention_without_gene_or_dna_inference(self):
        for mention in ("H3 K27M-mutant", "BRAF V600E mutation", "V600E-mutant", "p53 R273H mutations"):
            with self.subTest(mention=mention):
                packet, entity = self.packet_and_entity(mention, "biomarker")
                result = refine(packet, [entity], [])
                self.assertEqual(result["entities"][0]["type"], "variant")
                self.assertEqual(result["changes"][0]["rule_id"], VARIANT_RULE)
                self.assertEqual(result["entities"][0]["text"], mention)
                self.assertEqual(result["entities"][0]["locations"], entity["locations"])
                self.assertFalse(result["entities"][0]["type_refinement"]["new_standard_id_inferred"])
                self.assertIn("no DNA change", result["changes"][0]["interpretation"])
        for mention in ("H3 K27-altered", "KRAS comutation", "H3 K27M-mutant expression",
                        "H3 K27M mutation signature", "EXPRESSION V600E-mutant", "V600E", "H3 K27M-mutant DMG"):
            with self.subTest(mention=mention):
                packet, entity = self.packet_and_entity(mention, "biomarker")
                self.assertEqual(refine(packet, [entity], [])["entities"], [entity])
        packet, entity = self.packet_and_entity("H3 K27M-mutant", "disease")
        self.assertEqual(refine(packet, [entity], [])["entities"], [entity])

    def test_retyped_entity_revokes_unrevalidated_link_but_keeps_original_history(self):
        packet, entity = self.packet_and_entity("H3 K27M-mutant", "biomarker")
        entity["link"] = {"candidate_id": "NCBIGene:1", "proposed_id": "NCBIGene:1", "method": "old"}
        entity["candidates"] = {"kind": "biomarker", "candidates": [{"id": "NCBIGene:1"}]}
        before = deepcopy(entity)
        result = refine(packet, [entity], [])
        typed = result["entities"][0]
        self.assertIsNone(typed["link"]["candidate_id"])
        self.assertIsNone(typed["link"]["proposed_id"])
        self.assertEqual(typed["candidates"], entity["candidates"])
        self.assertEqual(typed["candidate_guards"], entity["candidate_guards"])
        self.assertEqual(result["original_entities"][0], before)
        self.assertEqual(entity, before)

    def test_unlocated_entity_is_unchanged_and_bad_coordinates_raise(self):
        packet, entity = self.packet_and_entity("H3 K27M-mutant", "biomarker")
        entity["locations"] = []
        self.assertEqual(refine(packet, [entity], [])["entities"], [entity])
        entity["locations"] = [{"passage_id": "p1", "start": 0, "end": 2}]
        with self.assertRaisesRegex(ValueError, "Invalid original entity location"):
            refine(packet, [entity], [])

    def test_gene_list_retypes_only_explicit_same_source_unlinked_drug_symbols(self):
        source = "OBSL1, BLM, and CCR1 were identified as candidate response-associated genes."
        packet, entity = self.packet_and_entity("OBSL1", "drug", source)
        original = deepcopy(entity)
        typed = refine(packet, [entity], packet["passages"])
        self.assertEqual(typed["entities"][0]["type"], "gene")
        self.assertEqual(typed["changes"][0]["rule_id"], GENE_RULE)
        self.assertEqual(typed["entities"][0]["locations"], original["locations"])
        self.assertEqual(entity, original)
        entity["link"]["proposed_id"] = "RXNORM:old-drug-candidate"
        typed = refine(packet, [entity], packet["passages"])
        self.assertIsNone(typed["entities"][0]["link"]["proposed_id"])
        self.assertEqual(typed["changes"][0]["standard_link_action"], "revoked_pending_type_compatible_revalidation")

    def test_gene_rule_does_not_guess_from_expression_case_proteins_or_other_versions(self):
        for source in ("OBSL1 expression was elevated.", "OBSL1, BLM, and CCR1 were identified as candidate proteins.",
                       "Patients received OBSL1."):
            packet, entity = self.packet_and_entity("OBSL1", "drug", source)
            self.assertEqual(refine(packet, [entity], packet["passages"])["entities"], [entity])
        source = "OBSL1, BLM, and CCR1 were identified as candidate response-associated genes."
        packet, entity = self.packet_and_entity("OBSL1", "protein", source)
        self.assertEqual(refine(packet, [entity], packet["passages"])["entities"], [entity])
        packet, entity = self.packet_and_entity("OBSL1", "drug", source)
        other = self.passage(source, "foreign", version_id="other")
        self.assertEqual(refine(packet, [entity], [other])["entities"], [entity])
        entity["link"]["candidate_id"] = "RXNORM:ingredient"
        self.assertEqual(refine(packet, [entity], packet["passages"])["entities"], [entity])

    @unittest.skipUnless(Path("var/experiments/evidence-passages-20261023/inputs.json").exists(), "Local frozen research artifacts unavailable")
    def test_frozen_15_groups_have_only_two_source_backed_changes(self):
        from scripts.refine_source_entity_types import BASE, CONTEXT, build

        result = build(BASE / "source-inputs.json", BASE / "assembled.json", CONTEXT)
        self.assertEqual(result["counts"]["entities_checked"], 173)
        self.assertEqual(result["counts"]["untouched"], 171)
        self.assertEqual([c["entity_id"] for r in result["records"] for c in r["changes"]], ["K08.e4", "K15.e21"])
        for row in result["records"]:
            changed = {c["entity_id"] for c in row["changes"]}
            for before, after in zip(row["original_entities"], row["entities"], strict=True):
                if before["id"] not in changed:
                    self.assertEqual(before, after)
                self.assertEqual(before["text"], after["text"])
                self.assertEqual(before["locations"], after["locations"])


if __name__ == "__main__":
    unittest.main()
