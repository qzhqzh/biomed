import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_linked_target_repair import _alias_ref, repair
from apps.curation.test_knowledge_target_mapping import DRUG, TUMOR
from apps.curation.test_source_linked_facts import SourceLinkedFactTests
from apps.curation.test_source_linked_target_mapping import (
    SourceLinkedTargetPreviewTests,
)


class SourceLinkedRepairTests(unittest.TestCase):
    def setUp(self):
        fixture = SourceLinkedFactTests()
        fixture.setUp()
        self.sample = fixture.sample
        self.payload = fixture.payload
        record, document = self.sample["record"], self.sample["source_document"]
        first = "Patients with advanced Cancer X (CX) harboring TEST mutations, including V1A, were enrolled."
        second = "For patients with V1A, median PFS for each first-line therapy: Drug A: 24.9 months; Drug B: 15.7 months."
        for index, text in enumerate((first, second)):
            document["passages"][index]["text"] = text
            record["anchors"][index].update(quote=text, end=len(text))
        document["passages_sha256"] = digest(document["passages"])
        document["metadata"] = {"publication_types": ["Journal Article"]}
        record["article"] = {"pmid": "fixture"}
        record["entities"] = [
            {"ref": "disease", "kind": "disease", "label": "Cancer X"},
            {"ref": "alias", "kind": "disease", "label": "CX"},
            {"ref": "gene", "kind": "gene", "label": "TEST"},
            {"ref": "variant", "kind": "variant", "label": "V1A"},
            {"ref": "drug", "kind": "drug", "label": "Drug A"},
        ]
        record["fact"] = {"relation": "treatment_outcome", "population": "Cancer X with V1A mutation",
                          "treatment": "Drug A", "conditions": ["V1A mutation"], "comparator": None,
                          "results": [{"value": "24.9 months", "measure": "PFS", "group": "V1A patients receiving Drug A", "passage": "P2"}],
                          "entity_ids": ["disease", "alias", "variant", "drug"],
                          "entity_roles": {"disease": ["disease"], "alias": ["disease"], "gene": ["context"],
                                           "variant": ["molecular"], "drug": ["intervention"]}, "evidence": ["P1", "P2"]}
        record["assessment"] = {"execution": {"status": "complete", "issues": []}, "quality": {"status": "unreviewed"}}
        self.sample["original_mapping"] = {"basis": "fixture", "entity_ids": {"disease": TUMOR, "alias": TUMOR, "drug": DRUG},
                                           "unresolved_entity_refs": [], "identity_diagnostics": {}}
        self.sample["original_export"] = {"offline_ready": False, "compatibility_status": "pending_mapping"}
        self.dictionary = {"schema_version": "tumor-api-dictionary-v1", "source": {"snapshot_id": "fixture"},
                           "drugs": [{"id": DRUG, "name": "Drug A", "aliases": []}],
                           "tumors": [{"id": TUMOR, "name": "Cancer X", "aliases": []}], "rules": [],
                           "logical_targets": [{"event_type": "mutation", "gene_symbol": "TEST", "target_key": "V1A"}]}
        self.standard = {"source": {"sha256": "c" * 64}, "symbols": {
            "TEST": {"symbol": "TEST", "gene_id": "1", "hgnc_id": "2"},
            "OTHER": {"symbol": "OTHER", "gene_id": "3", "hgnc_id": "4"}}}
        fact = self.payload["facts"][0]
        fact["population"] = {"text": "Patients with advanced Cancer X (CX) harboring V1A mutation", "evidence": ["P1"]}
        fact["disease"] = {"text": "Cancer X", "entity_ref": "disease", "evidence": ["P1"]}
        fact["molecular_event"].update(gene="TEST", event="V1A", entity_refs=["gene", "variant"], evidence=["P1"])
        fact["intervention"] = {"text": "Drug A", "entity_ref": "drug", "role": "current", "evidence": ["P2"]}
        fact["outcome"] = {"text": "median PFS of 24.9 months", "evidence": ["P2"]}
        fact.update(same_cohort=True, cohort_evidence=["P2"], conditions=["first-line therapy", "V1A mutation"])

    def run_repair(self):
        return repair(self.sample, self.payload, dictionary=self.dictionary, gene_standard=self.standard,
                      disease_nodes=[], ontology_source={"sha256": "d" * 64})

    def replace_quote(self, index, text):
        self.sample["record"]["anchors"][index].update(quote=text, end=len(text))
        self.sample["source_document"]["passages"][index]["text"] = text
        self.sample["source_document"]["passages_sha256"] = digest(self.sample["source_document"]["passages"])

    def test_repairs_gene_alias_and_conditions_without_changing_original_or_automatic_matching(self):
        before = copy.deepcopy(self.sample)
        output = self.run_repair()
        self.assertEqual(output["status"], "offline_structure_only")
        self.assertEqual(self.sample, before)
        self.assertFalse(output["automatic_clinical_matching"])
        export = output["derived_export"]
        self.assertTrue(export["offline_ready"])
        self.assertFalse(export["target_payload"]["is_active"])
        condition = export["target_payload"]["conclusion_inputs"][0]["trigger_inputs"][0]
        self.assertEqual((condition["gene_symbol"], condition["target_key"]), ("TEST", "V1A"))
        self.assertIn("first-line therapy", export["biomed_companion"]["fact"]["conditions"])
        self.assertIn("Patients with advanced Cancer X (CX)", export["biomed_companion"]["fact"]["conditions"])
        self.assertEqual(export["biomed_companion"]["fact"]["results"][0]["measure"], "median PFS")
        self.assertIn("first-line therapy", export["biomed_companion"]["fact"]["results"][0]["group"])
        self.assertEqual(export["biomed_companion"]["assessment"]["compatibility"]["status"], export["compatibility_status"])
        self.assertEqual(export["biomed_companion"]["assessment"]["compatibility"]["contract_validation"], export["contract_validation"])
        self.assertFalse(export["adaptation"]["trigger_scope"]["clinical_context_enforced_by_target"])

    def test_gene_from_other_gene_list_is_not_paired_with_variant(self):
        self.replace_quote(0, "Patients with advanced Cancer X (CX) harboring TEST mutations, including V2A, and OTHER mutations including V1A, were enrolled.")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_unrelated_old_fact_anchor_is_kept_in_provenance_but_not_as_direct_evidence(self):
        record, document = self.sample["record"], self.sample["source_document"]
        text = "A different compound mutation was investigated in a simulation."
        passage = {"id": "77777777-7777-4777-8777-777777777777", "version_id": record["version_id"],
                   "locator": "body/p:3", "text": text}
        document["passages"].append(passage)
        document["passages_sha256"] = digest(document["passages"])
        record["anchors"].append({"passage_id": passage["id"], "start": 0, "end": len(text), "quote": text,
                                  "purpose": "evidence", "attributes": {"alias": "P3"}})
        record["fact"]["evidence"].append("P3")
        result = self.run_repair()
        self.assertEqual(result["status"], "offline_structure_only")
        derived = result["derived_export"]["biomed_companion"]
        self.assertNotIn("P3", derived["fact"]["evidence"])
        self.assertIn("P3", derived["provenance"][result["repair_version"]]["original_fact"]["evidence"])

    def test_negative_mutation_is_not_a_positive_trigger(self):
        self.replace_quote(0, "Patients with advanced Cancer X (CX) harboring TEST mutations, including V1A, were not enrolled.")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_alias_must_share_actual_target_identity_and_source_definition(self):
        self.sample["original_mapping"]["entity_ids"]["alias"] = DRUG
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_uniquely_recorded_gene_alias_keeps_original_spelling_and_canonical_target(self):
        self.standard["aliases"] = {"TESTOLD": "TEST"}
        gene = next(e for e in self.sample["record"]["entities"] if e["ref"] == "gene")
        gene["label"] = "TESTOLD"
        self.payload["facts"][0]["molecular_event"]["gene"] = "TESTOLD"
        self.replace_quote(0, "Patients with advanced Cancer X (CX) harboring TESTOLD mutations, including V1A, were enrolled.")
        result = self.run_repair()
        self.assertEqual(result["status"], "offline_structure_only")
        self.assertEqual(result["derived_export"]["target_payload"]["conclusion_inputs"][0]["trigger_inputs"][0]["gene_symbol"], "TEST")
        self.assertEqual(gene["label"], "TESTOLD")

    def test_explicit_retrospective_study_scope_is_preserved(self):
        self.replace_quote(0, "This retrospective study analyzed patients with advanced Cancer X (CX) harboring TEST mutations, including V1A, who were enrolled.")
        result = self.run_repair()
        self.assertEqual(result["status"], "offline_structure_only")
        self.assertIn("retrospective study", result["derived_export"]["biomed_companion"]["fact"]["scope"])

    def test_typographic_disease_alias_definition_can_match(self):
        for entity in self.sample["record"]["entities"]:
            if entity["ref"] == "disease":
                entity["label"] = "non-small cell lung cancer"
            elif entity["ref"] == "alias":
                entity["label"] = "NSCLC"
        self.sample["record"]["fact"]["population"] = "NSCLC with V1A mutation"
        self.dictionary["tumors"][0]["name"] = "non-small cell lung cancer"
        fact = self.payload["facts"][0]
        fact["disease"]["text"] = "non-small cell lung cancer"
        fact["population"]["text"] = "Patients with advanced non–small-cell lung cancer (NSCLC) harboring V1A mutation"
        self.replace_quote(0, "Patients with advanced non–small-cell lung cancer (NSCLC) harboring TEST mutations, including V1A, were enrolled.")
        entities = {row["ref"]: row for row in self.sample["record"]["entities"]}
        self.assertEqual(_alias_ref(self.sample["record"], self.sample["original_mapping"], "disease", entities), ["alias"])
        # The source-linked model's stricter literal disease mention is its own
        # independent gate; alias normalization never bypasses that gate.
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_do_not_drop_other_molecular_conditions_or_comparator(self):
        self.sample["record"]["fact"]["conditions"].append("V2A mutation")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")
        self.sample["record"]["fact"]["conditions"].pop()
        self.sample["record"]["fact"]["comparator"] = "Drug B"
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_combined_treatment_and_review_are_not_promoted(self):
        self.replace_quote(1, "For patients with V1A, median PFS for each first-line therapy: Drug A and Drug B: 24.9 months.")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")
        self.setUp()
        self.sample["source_document"]["metadata"]["publication_types"].append("Review")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_other_regimen_result_is_not_attributed_to_selected_drug(self):
        self.sample["record"]["entities"].append({"ref": "other-drug", "kind": "drug", "label": "Drug B"})
        self.sample["record"]["fact"]["entity_roles"]["other-drug"] = ["context"]
        self.replace_quote(1, "For patients with V1A, median PFS for first-line therapy: Drug A, whereas Drug B reached 24.9 months.")
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_missing_first_line_evidence_cannot_be_assumed(self):
        self.payload["facts"][0]["conditions"] = ["V1A mutation"]
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_conflicting_treatment_line_is_not_rewritten(self):
        self.sample["record"]["fact"]["results"][0]["group"] = "patients receiving second-line Drug A"
        self.assertNotEqual(self.run_repair()["status"], "offline_structure_only")

    def test_previous_preview_refreshes_only_derived_compatibility_metadata(self):
        previous = SourceLinkedTargetPreviewTests()
        previous.setUp()
        previous.sample["record"]["assessment"]["compatibility"] = {"status": "pending_mapping", "contract_validation": {"valid": False}}
        before = copy.deepcopy(previous.sample)
        result = repair(previous.sample, previous.payload, dictionary=previous.dictionary, gene_standard=previous.standard,
                        disease_nodes=[], ontology_source={"sha256": "d" * 64})
        self.assertEqual(result["status"], "offline_structure_only")
        self.assertEqual(previous.sample, before)
        export = result["derived_export"]
        self.assertEqual(export["biomed_companion"]["assessment"]["compatibility"]["status"], "offline_validated")
        self.assertTrue(export["biomed_companion"]["assessment"]["compatibility"]["contract_validation"]["valid"])
        old = export["biomed_companion"]["provenance"][result["repair_version"]]["original_compatibility"]
        self.assertEqual(old["status"], "pending_mapping")

    def case_fixture(self, regimen):
        previous = SourceLinkedTargetPreviewTests()
        previous.setUp()
        record, document = previous.sample["record"], previous.sample["source_document"]
        record["fact"].update(scope="original-study/case-report",
                              population="patient with Cancer X harboring TEST V1A mutation",
                              treatment="Drug A 100 mg")
        document["passages"].append({"id": "77777777-7777-4777-8777-777777777777", "version_id": record["version_id"],
                                     "locator": "body/p:3", "text": regimen})
        document["passages_sha256"] = digest(document["passages"])
        return previous

    def test_case_regimen_is_enriched_from_own_article_without_changing_original(self):
        case = self.case_fixture("The patient was started on Drug A at 100 mg by mouth daily.")
        before = copy.deepcopy(case.sample)
        result = repair(case.sample, case.payload, dictionary=case.dictionary, gene_standard=case.standard,
                        disease_nodes=[], ontology_source={"sha256": "d" * 64})
        self.assertEqual(case.sample, before)
        self.assertEqual(result["repair_status"], "source_regimen_enriched")
        export = result["derived_export"]
        self.assertEqual(export["biomed_companion"]["fact"]["treatment"], "Drug A 100 mg by mouth daily")
        provenance = export["biomed_companion"]["provenance"][result["repair_version"]]["source_regimen_enrichment"]
        self.assertEqual(provenance["original_fact"]["treatment"], "Drug A 100 mg")
        self.assertEqual(provenance["dose_source"]["passage_id"], "77777777-7777-4777-8777-777777777777")
        self.assertFalse(export["adaptation"]["treatment_scope"]["dose_enforced_by_target"])
        self.assertFalse(result["automatic_clinical_matching"])

    def test_case_regimen_does_not_borrow_wrong_dose_or_other_subject(self):
        for sentence in ("The patient was started on Drug A at 200 mg by mouth daily.",
                         "Another patient was started on Drug A at 100 mg by mouth daily.",
                         "The patient was started on Drug A at 100 mg intravenously daily."):
            with self.subTest(sentence=sentence):
                case = self.case_fixture(sentence)
                result = repair(case.sample, case.payload, dictionary=case.dictionary, gene_standard=case.standard,
                                disease_nodes=[], ontology_source={"sha256": "d" * 64})
                self.assertEqual(result["repair_status"], "baseline_preserved")
                self.assertEqual(result["derived_export"]["biomed_companion"]["fact"]["treatment"], "Drug A 100 mg")


if __name__ == "__main__":
    unittest.main()
