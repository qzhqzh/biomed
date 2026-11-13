import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_linked_target_mapping import preview
from apps.curation.test_knowledge_target_mapping import DRUG, TUMOR
from apps.curation.test_source_linked_facts import SourceLinkedFactTests


class SourceLinkedTargetPreviewTests(unittest.TestCase):
    def setUp(self):
        fixture = SourceLinkedFactTests()
        fixture.setUp()
        self.sample = fixture.sample
        self.payload = fixture.payload
        record = self.sample["record"]
        record["article"] = {"pmid": "fixture"}
        record["fact"] = {
            "relation": "treatment_outcome", "population": "Cancer X harboring TEST V1A mutation",
            "conditions": ["TEST V1A mutation"], "treatment": "Drug A", "drug_names": ["Drug A"],
            "entity_roles": {"disease": ["disease"], "variant": ["molecular"], "drug": ["intervention"]},
            "results": [{"value": "partial response", "measure": "response", "passage": "P2"}],
        }
        record["assessment"] = {"execution": {"status": "partial", "issues": ["target_anchor_not_cited"]},
                                "quality": {"status": "unreviewed"}}
        self.sample["original_mapping"] = {
            "basis": "fixture", "entity_ids": {"drug": DRUG, "disease": TUMOR},
            "unresolved_entity_refs": [], "identity_diagnostics": {},
        }
        self.sample["original_export"] = {"offline_ready": False, "compatibility_status": "pending_mapping"}
        self.dictionary = {"schema_version": "tumor-api-dictionary-v1", "source": {"snapshot_id": "fixture"},
                           "drugs": [{"id": DRUG, "name": "Drug A", "aliases": []}],
                           "tumors": [{"id": TUMOR, "name": "Cancer X", "aliases": []}],
                           "rules": [], "logical_targets": [{"event_type": "mutation", "gene_symbol": "TEST", "target_key": "V1A"}]}
        self.standard = {"source": {"sha256": "c" * 64},
                         "symbols": {"TEST": {"symbol": "TEST", "gene_id": "1", "hgnc_id": "2"}}}

    def run_preview(self):
        return preview(self.sample, self.payload, dictionary=self.dictionary, gene_standard=self.standard,
                       disease_nodes=[], ontology_source={"sha256": "d" * 64})

    def replace_first_quote(self, quote):
        record, doc = self.sample["record"], self.sample["source_document"]
        record["anchors"][0].update(quote=quote, end=len(quote))
        doc["passages"][0]["text"] = quote
        doc["passages_sha256"] = digest(doc["passages"])

    def test_preview_preserves_original_and_context_without_claiming_semantic_truth(self):
        before = copy.deepcopy(self.sample)
        result = self.run_preview()
        self.assertEqual(result["status"], "offline_structure_only")
        self.assertEqual(self.sample, before)
        self.assertTrue(result["derived_export"]["offline_ready"])
        self.assertFalse(result["automatic_clinical_matching"])
        self.assertFalse(result["derived_export"]["target_payload"]["is_active"])
        self.assertEqual(result["semantic_status"], "unreviewed")
        provenance = result["derived_export"]["biomed_companion"]["provenance"][result["version"]]
        self.assertEqual(provenance["original_execution"]["issues"], ["target_anchor_not_cited"])
        self.assertIn("TEST V1A mutation", result["derived_export"]["adaptation"]["trigger_scope"]["unstructured_clinical_context"])

    def test_old_ready_record_is_preserved_even_when_new_output_is_bad(self):
        self.sample["original_export"]["offline_ready"] = True
        self.payload = None
        result = self.run_preview()
        self.assertEqual(result["status"], "baseline_preserved")
        self.assertIsNone(result["derived_export"])
        self.assertTrue(result["original_export"]["offline_ready"])

    def test_protocol_prior_and_absent_events_are_not_promoted(self):
        for field, value in (("source_scope", "protocol"), ("same_cohort", False)):
            with self.subTest(field=field):
                original = self.payload["facts"][0][field]
                self.payload["facts"][0][field] = value
                self.assertEqual(self.run_preview()["status"], "deferred")
                self.payload["facts"][0][field] = original
        for field, value in (("status", "absent"), ("role", "background")):
            with self.subTest(field=field):
                original = self.payload["facts"][0]["molecular_event"][field]
                self.payload["facts"][0]["molecular_event"][field] = value
                self.assertEqual(self.run_preview()["status"], "deferred")
                self.payload["facts"][0]["molecular_event"][field] = original
        self.payload["facts"][0]["intervention"]["role"] = "prior"
        self.assertEqual(self.run_preview()["status"], "deferred")

    def test_different_group_and_negative_source_are_not_promoted(self):
        for quote in (
            "Patients with Cancer X harboring TEST V1A were observed. Other patients received Drug A.",
            "Patients with Cancer X without TEST V1A received Drug A.",
            "Patients with Cancer X harboring TEST V1A and V2A received Drug A.",
            "Patients with Cancer X harboring TEST V1A received Drug A plus Drug B.",
        ):
            with self.subTest(quote=quote):
                self.replace_first_quote(quote)
                self.assertEqual(self.run_preview()["status"], "deferred")

    def test_uncited_or_invented_outcome_and_other_execution_errors_stay_deferred(self):
        self.payload["facts"][0]["outcome"]["evidence"] = ["P1"]
        self.assertEqual(self.run_preview()["status"], "deferred")
        self.payload["facts"][0]["outcome"]["evidence"] = ["P2"]
        self.payload["facts"][0]["outcome"]["text"] = "complete response"
        self.assertEqual(self.run_preview()["status"], "deferred")
        self.payload["facts"][0]["outcome"]["text"] = "partial response"
        self.sample["record"]["assessment"]["execution"]["issues"].append("invalid_evidence")
        self.assertEqual(self.run_preview()["status"], "deferred")

    def test_extra_condition_or_unknown_identity_is_not_silently_resolved(self):
        self.sample["record"]["fact"]["molecular_logic"] = "and"
        self.assertEqual(self.run_preview()["status"], "deferred")
        del self.sample["record"]["fact"]["molecular_logic"]
        self.sample["record"]["fact"]["conditions"].append("PD-L1 expression")
        self.assertEqual(self.run_preview()["status"], "deferred")
        self.sample["record"]["fact"]["conditions"].pop()
        self.standard["symbols"]["OTHER"] = {"symbol": "OTHER", "gene_id": "3", "hgnc_id": "4"}
        self.sample["record"]["fact"]["conditions"].append("OTHER mutation")
        self.assertEqual(self.run_preview()["status"], "deferred")
        self.sample["record"]["fact"]["conditions"].pop()
        self.sample["original_mapping"]["unresolved_entity_refs"] = ["disease"]
        self.sample["original_mapping"]["identity_diagnostics"]["disease"] = {"status": "local_identity_ambiguous"}
        self.assertEqual(self.run_preview()["status"], "deferred")


if __name__ == "__main__":
    unittest.main()
