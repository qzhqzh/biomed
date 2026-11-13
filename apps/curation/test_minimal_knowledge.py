import copy
import unittest

from apps.curation.minimal_knowledge import build_bundle, inventory_roles, normalize


class MinimalKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.article = {"pmid": "1", "version_id": "v1", "source_sha256": "a" * 64}
        self.packet = {"id": "T1", "article": self.article, "anchor_id": "p1", "passages": [{
            **self.article, "id": "p1", "parser_version": "test", "locator": "p:1", "role": "anchor", "alias": "P1",
            "text": "Cancer X patients received Drug A without concurrent Drug B. biliary tract cancer (BTC).",
        }]}
        self.fact = {"relation": "treatment_outcome", "conclusion": "Drug A showed activity.",
                     "population": "Cancer X", "treatment": "Drug A without concurrent Drug B", "comparator": None,
                     "conditions": [], "scope": "human", "evidence": ["P1"], "entities": [
                         {"text": "Cancer X", "type": "disease", "passage": "P1", "roles": ["disease"]},
                         {"text": "Drug A", "type": "drug", "passage": "P1", "roles": ["intervention"]},
                         {"text": "Drug B", "type": "drug", "passage": "P1", "roles": ["intervention"]},
                     ]}

    def result(self):
        return {"status": "classified", "payload": {"fact": self.fact, "reason": None}}

    def test_literal_sources_and_negated_drug_not_current_intervention(self):
        original = copy.deepcopy(self.packet)
        view = normalize(self.packet, self.result(), mode="direct")
        self.assertEqual(view["execution_status"], "complete")
        self.assertEqual(view["fact"]["entity_roles"]["T1.e3"], ["context"])
        self.assertEqual(view["fact"]["drug_names"], ["Drug A"])
        for anchor in view["anchors"]:
            self.assertEqual(self.packet["passages"][0]["text"][anchor["start"]:anchor["end"]], anchor["quote"])
        self.assertEqual(self.packet, original)

    def test_background_inventory_does_not_enter_relationship(self):
        entity = {"id": "e1", "type": "drug", "text": "Other Drug"}
        self.assertEqual(inventory_roles(entity, self.fact, self.packet), ["context"])

    def test_source_exact_compound_disease_is_split_without_rewriting_model(self):
        mention = "KRAS p.G12C–mutated advanced NSCLC"
        self.packet["passages"][0]["text"] = mention + " patients received Drug A."
        self.packet["entities"] = [{"id": "d1", "type": "disease", "text": mention,
                                   "locations": [{"passage_id": "p1", "start": 0, "end": len(mention)}]}]
        self.fact.update(population=mention, entity_ids=["d1"])
        before = copy.deepcopy((self.packet, self.fact))
        view = normalize(self.packet, self.result(), mode="inventory")
        self.assertEqual(view["fact"]["entity_roles"], {"d1": ["context"], "d1.variant": ["molecular"], "d1.disease": ["disease"]})
        self.assertEqual([e["label"] for e in view["entities"]], [mention, "KRAS p.G12C", "NSCLC"])
        for anchor in view["anchors"]:
            self.assertEqual(self.packet["passages"][0]["text"][anchor["start"]:anchor["end"]], anchor["quote"])
        self.assertEqual((self.packet, self.fact), before)

    def test_multiple_facts_keep_separate_conditions_and_common_lineage(self):
        other = copy.deepcopy(self.fact)
        other["conditions"] = ["ctDNA clearance"]
        raw = {"status": "classified", "payload": {"facts": [self.fact, other], "reason": None}}
        bundle, report = build_bundle([self.packet], {"T1": raw}, source_run="test")
        self.assertEqual([r["group_id"] for r in bundle["records"]], ["T1", "T1.f02"])
        self.assertEqual([r["fact"]["conditions"] for r in bundle["records"]], [[], ["ctDNA clearance"]])
        self.assertEqual({r["provenance"]["source_packet_id"] for r in bundle["records"]}, {"T1"})
        self.assertEqual(report["source_packets"], 1)
        raw["payload"]["facts"] *= 2
        failed, _ = build_bundle([self.packet], {"T1": raw}, source_run="test")
        self.assertEqual(len(failed["records"]), 1)
        self.assertEqual(failed["records"][0]["assessment"]["execution"]["status"], "failed")

    def test_absence_of_response_is_not_absence_of_treatment(self):
        self.fact["treatment"] = "Drug A without response to Drug B"
        view = normalize(self.packet, self.result(), mode="direct")
        self.assertIn("intervention", view["fact"]["entity_roles"]["T1.e3"])

    def test_compact_hgvs_matches_same_gene_and_variant_only(self):
        fact = {"population": "KRASp.G12C mutation in NSCLC"}
        for kind, label in (("variant", "KRAS G12C"), ("gene", "KRAS")):
            self.assertEqual(inventory_roles({"type": kind, "text": label}, fact), ["molecular"])
        for label in ("KRAS G12D", "NRAS G12C"):
            self.assertEqual(inventory_roles({"type": "variant", "text": label}, fact), ["context"])

    def test_negation_does_not_cross_between_drug_names(self):
        self.fact["treatment"] = "Drug A was given and Drug B was not administered"
        view = normalize(self.packet, self.result(), mode="direct")
        self.assertIn("intervention", view["fact"]["entity_roles"]["T1.e2"])
        self.assertEqual(view["fact"]["entity_roles"]["T1.e3"], ["context"])

    def test_explicit_source_acronym_can_match_population(self):
        self.assertEqual(inventory_roles({"type": "disease", "text": "BTC"},
                         {"population": "biliary tract cancer"}, self.packet), ["disease"])
        self.assertEqual(inventory_roles({"type": "disease", "text": "ABC"},
                         {"population": "biliary tract cancer"}, self.packet), ["context"])

    def test_script_locates_real_quote_despite_model_passage_typo(self):
        self.fact["entities"][0]["passage"] = "P2"
        view = normalize(self.packet, self.result(), mode="direct")
        self.assertNotIn("unlocated_participant", view["issues"])
        anchor = next(a for a in view["anchors"] if a["entity_ref"] == "T1.e1")
        self.assertEqual(anchor["passage_id"], "p1")
        self.assertEqual(anchor["attributes"]["model_passage"], "P2")

    def test_absent_mention_is_not_invented(self):
        self.fact["entities"][0]["text"] = "Unmentioned cancer"
        view = normalize(self.packet, self.result(), mode="direct")
        self.assertIn("unlocated_participant", view["issues"])
        self.assertNotIn("T1.e1", view["fact"]["entity_ids"])

    def test_only_selected_single_disease_not_all_inventory(self):
        self.packet["entities"] = [{"id": "d1", "type": "disease", "text": "BTC",
                                     "locations": [{"passage_id": "p1", "start": 80, "end": 83}]}]
        start = self.packet["passages"][0]["text"].index("BTC")
        self.packet["entities"][0]["locations"][0].update(start=start, end=start+3)
        self.fact.pop("entities")
        self.fact.update(entity_ids=["d1"], population="experimental model")
        view = normalize(self.packet, self.result(), mode="inventory")
        self.assertEqual(view["fact"]["entity_roles"]["d1"], ["disease"])
        self.fact["entity_ids"] = []
        view = normalize(self.packet, self.result(), mode="inventory")
        self.assertEqual(view["fact"]["entity_roles"]["d1"], ["context"])

    def test_failed_output_saved_without_fabricated_knowledge(self):
        bundle, _report = build_bundle([self.packet], {"T1": {"status": "output_truncated", "payload": None}}, source_run="test")
        record = bundle["records"][0]
        self.assertEqual(record["assessment"]["execution"]["status"], "failed")
        self.assertEqual(record["fact"], {})
        self.assertEqual(record["assessment"]["quality"]["status"], "unreviewed")
        self.assertEqual(len(record["anchors"]), 1)

    def test_class_guard_changes_type_not_ingredient_identity(self):
        def linker(entity, packet):
            return {"link": {"candidate_id": None}, "candidate_guards": {"x": {
                "reasons": [{"code": "ingredient_class_ambiguity"}]}}}
        view = normalize(self.packet, self.result(), mode="direct", linker=linker)
        drug = next(e for e in view["entities"] if e["label"] == "Drug A")
        self.assertEqual((drug["kind"], drug["subtype"]), ("treatment", "drug_class"))

    def test_source_gene_retyping_restores_only_literal_fact_participants(self):
        text = "GENE1, GENE2, and GENE3 were identified as candidate response-associated genes."
        self.packet["passages"][0]["text"] = text
        self.packet["entities"] = []
        for number, label in enumerate(("GENE1", "GENE2", "GENE3"), 1):
            start = text.index(label)
            self.packet["entities"].append({"id": f"e{number}", "type": "drug", "text": label,
                "locations": [{"passage_id": "p1", "start": start, "end": start + len(label)}]})
        self.fact.pop("entities")
        self.fact.update(relation="biomarker_association", conclusion=None, population=None, treatment=None,
                         entity_ids=[], results=[{"group": "GENE1 + GENE2 model", "value": "associated with response"}])
        original = copy.deepcopy((self.packet, self.result()))

        def linker(entity, packet):
            return {"link": {"candidate_id": None, "proposed_id": "RXNORM:unconfirmed"}}

        view = normalize(self.packet, self.result(), mode="inventory", linker=linker)
        self.assertEqual([e["kind"] for e in view["entities"]], ["gene"] * 3)
        self.assertEqual(view["fact"]["entity_roles"], {"e1": ["molecular"], "e2": ["molecular"], "e3": ["context"]})
        self.assertEqual((self.packet, self.result()), original)
        for anchor in view["anchors"]:
            if anchor["entity_ref"]:
                self.assertIsNone(anchor["attributes"]["standard_link"]["proposed_id"])
        self.assertEqual(view["type_changes"][0]["role_projection"]["after"], ["molecular"])
