import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from apps.curation.knowledge_export import export_bundle
from apps.curation.knowledge_target_mapping import (
    GENE_STANDARD_SCHEMA,
    REGISTRY_SCHEMA,
    prepare_mappings,
)

DRUG = "11111111-1111-4111-8111-111111111111"
TUMOR = "22222222-2222-4222-8222-222222222222"
SUBTYPE = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
PASSAGE = "55555555-5555-4555-8555-555555555555"
LOCAL_MAIN = "66666666-6666-4666-8666-666666666666"
LOCAL_SUBTYPE = "77777777-7777-4777-8777-777777777777"
LOCAL_DRUG = "88888888-8888-4888-8888-888888888888"


class KnowledgeTargetMappingTests(unittest.TestCase):
    def setUp(self):
        self.quote = "Patients with Cancer X harboring TEST V1A mutation were treated with Drug A."
        self.record = {
            "group_id": "record-1", "version_id": VERSION, "source_sha256": "a" * 64,
            "article": {"pmid": "synthetic"},
            "fact": {"relation": "treatment_outcome", "population": "Cancer X harboring TEST V1A mutation",
                     "conditions": ["TEST V1A mutation"], "treatment": "Drug A",
                     "entity_roles": {"drug": ["intervention"], "cancer": ["disease"], "variant": ["molecular"]}},
            "entities": [{"ref": "drug", "kind": "drug", "label": "Drug A"},
                         {"ref": "cancer", "kind": "disease", "label": "Cancer X"},
                         {"ref": "variant", "kind": "variant", "label": "TEST V1A"}],
            "anchors": [{"purpose": "evidence", "passage_id": PASSAGE, "start": 0,
                         "end": len(self.quote), "quote": self.quote}],
            "assessment": {"execution": {"status": "complete"}, "quality": {"status": "unreviewed"}},
        }
        self.bundle = {"schema_version": "knowledge-candidates-v1", "source_run": "synthetic", "records": [self.record]}
        self.dictionary = {"schema_version": "tumor-api-dictionary-v1", "source": {"snapshot_id": "synthetic"},
                           "drugs": [{"id": DRUG, "name": "Drug A", "aliases": []}],
                           "tumors": [{"id": TUMOR, "name": "Cancer X", "aliases": []}],
                           "rules": [], "logical_targets": [{"event_type": "mutation", "gene_symbol": "TEST", "target_key": "V1A"}]}
        self.gene_standard = {"schema_version": GENE_STANDARD_SCHEMA, "source": {"fixture": True, "sha256": "c" * 64},
                              "symbols": {"TEST": {"symbol": "TEST", "gene_id": "1", "hgnc_id": "2"}}}

    def result(self, registry=None):
        mappings, report = prepare_mappings(self.bundle, self.dictionary, registry, gene_standard=self.gene_standard)
        self.assertEqual(report["mapping_method_version"], "biomed.knowledge-target-mapping.v6")
        exported = export_bundle(self.bundle, dictionary=self.dictionary, mappings=mappings)
        return mappings["record-1"], report["records"][0], exported["records"][0]

    def replace_quote(self, value):
        self.record["anchors"][0].update(quote=value, end=len(value))

    def test_equivalent_cohort_receipt_wordings_and_doses(self):
        for quote in (
            "Participants with Cancer X harboring TEST V1A mutation were treated with Drug A.",
            "Patients with Cancer X harboring TEST V1A mutation received daily Drug A.",
            "Patients with Cancer X harboring TEST V1A mutation received Drug A at a dose of 100 mg once daily.",
            "Eligible patients had Cancer X harboring TEST V1A mutation. All enrolled patients received Drug A.",
            "Patients were eligible if they had Cancer X; a TEST V1A mutation; received Drug A 100 mg orally once daily until progression; signed consent.",
        ):
            with self.subTest(quote=quote):
                self.replace_quote(quote)
                self.assertTrue(self.result()[2]["offline_ready"])

    def test_enrollment_does_not_permit_absent_variant_or_combined_treatment(self):
        for quote in (
            "Patients were eligible if they had Cancer X; no TEST V1A mutation; received Drug A 100 mg daily; signed consent.",
            "Patients were eligible if they had Cancer X; a TEST V1A mutation; received Drug A and Drug B; signed consent.",
            "Eligible patients had Cancer X harboring TEST V1A mutation. Other patients received Drug A.",
            "Patients were eligible if they had Cancer X; a TEST V1A mutation; a TEST V2A mutation; received Drug A; signed consent.",
            "Patients were eligible if they had Cancer X; a TEST V1A mutation; PD-L1 expression; received Drug A; signed consent.",
        ):
            with self.subTest(quote=quote):
                self.replace_quote(quote)
                self.assertFalse(self.result()[2]["offline_ready"])

    def test_repeated_molecular_disease_condition_does_not_invent_a_second_trigger(self):
        self.make_trial_record()
        self.record["fact"].update(treatment="Drug A", conditions=["TEST V1A–mutated advanced NSCLC"])
        self.assertTrue(self.result()[2]["offline_ready"])

    def test_confirmed_type_correction_is_not_an_unknown_drug_or_a_new_target_drug(self):
        registry = {"schema_version": REGISTRY_SCHEMA, "source": {"fixture": True}, "entities": [],
                    "rules": [{"id": LOCAL_DRUG, "kind": "drug", "alias": "Drug A", "scope": {"type": "article", "key": VERSION},
                               "status": "confirmed", "action": "ignore", "canonical_id": None,
                               "reason": "Synthetic source defines a treatment regimen", "provenance": {"normalized_kind": "treatment"}}]}
        _, report, _ = self.result(registry)
        drug = next(row for row in report["entity_mappings"] if row["entity_ref"] == "drug")
        self.assertEqual(drug["status"], "not_target_entity")
        self.assertEqual(drug["normalized_kind"], "treatment")
        self.assertNotIn("entity_operation", drug)

    def make_trial_record(self):
        first = "A study discussed non–small-cell lung cancer (NSCLC)."
        trial = ("In a single-group, phase 2 trial, we investigated the activity of Drug A, administered orally at a dose of "
                 "960 mg once daily, in patients with TEST V1A–mutated advanced NSCLC previously treated with standard therapies.")
        outcomes = ("Among 100 patients, the majority (81.0%) had previously received both platinum-based chemotherapy and "
                    "inhibitors of programmed death 1 (PD-1) or programmed death ligand 1 (PD-L1). "
                    "According to central review, 98 patients had measurable disease at baseline and were evaluated for response.")
        self.dictionary["tumors"][0].update(name="Non-small cell lung cancer", aliases=["NSCLC"])
        self.record["entities"] = [{"ref": "drug", "kind": "drug", "label": "Drug A"},
                                   {"ref": "long", "kind": "disease", "label": "non–small-cell lung cancer"},
                                   {"ref": "short", "kind": "disease", "label": "NSCLC"},
                                   {"ref": "variant", "kind": "variant", "label": "TEST V1A"}]
        self.record["fact"].update(
            population="patients with TEST V1A–mutated advanced NSCLC previously treated with standard therapies",
            treatment="Drug A, administered orally at a dose of 960 mg once daily",
            conditions=["previously treated with standard therapies",
                        "81.0% had previously received both platinum-based chemotherapy and inhibitors of programmed death 1 (PD-1) or programmed death ligand 1 (PD-L1)",
                        "98 patients had measurable disease at baseline and were evaluated for response"],
            results=[{"measure": "response", "group": "patients with measurable disease", "value": "reported"}],
            entity_roles={"drug": ["intervention"], "long": ["disease"], "short": ["disease"], "variant": ["molecular"]})
        self.record["anchors"] = [{"purpose": purpose, "passage_id": passage, "start": 0, "end": len(quote), "quote": quote}
                                  for purpose, passage, quote in (("context", "99999999-9999-4999-8999-999999999999", first),
                                                                  ("evidence", PASSAGE, trial),
                                                                  ("evidence", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", outcomes))]

    def test_source_selected_variant_and_single_drug_are_offline_validated_without_modifying_fact(self):
        before = copy.deepcopy(self.bundle)
        mapping, report, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(mapping["entity_ids"], {"drug": DRUG, "cancer": TUMOR})
        self.assertEqual(mapping["trigger_bindings"][0]["input"],
                         {"trigger_type": "direct", "event_type": "mutation", "gene_symbol": "TEST", "target_key": "V1A"})
        self.assertEqual(exported["matching_validation"][0]["snapshot_consistency"], "consistent")
        self.assertEqual(exported["live_sample_matching"], "unverified")
        self.assertEqual(report["gaps"], [])
        self.assertEqual(self.bundle, before)
        self.assertEqual(exported["biomed_companion"], before["records"][0])
        self.assertFalse(exported["submit_ready"])

    def test_new_specific_key_is_validated_but_partial_snapshot_is_not_evidence_of_target_absence(self):
        self.dictionary["logical_targets"] = []
        mapping, report, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(exported["matching_validation"][0]["snapshot_consistency"], "not_covered")
        self.assertIn("snapshot_not_covering_key", exported["matching_validation"][0]["warnings"])
        self.assertIn("snapshot_not_covering_key", report["gaps"])
        self.assertEqual(len(mapping["trigger_bindings"]), 1)
        self.assertEqual(exported["live_sample_matching"], "unverified")

    def test_trial_dose_prior_therapy_and_same_disease_names_remain_in_source_fact(self):
        self.make_trial_record()
        original = copy.deepcopy(self.record)
        mapping, report, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(mapping["entity_ids"]["long"], TUMOR)
        self.assertEqual(mapping["entity_ids"]["short"], TUMOR)
        self.assertEqual(exported["target_payload"]["conclusion_inputs"][0]["disease_scope_inputs"], [{"tumor_id": TUMOR}])
        self.assertEqual(exported["biomed_companion"], original)
        self.assertIn("960 mg once daily", exported["target_payload"]["evidence_description"])
        self.assertIn("previously received both", exported["target_payload"]["evidence_description"])
        self.assertFalse(mapping["trigger_scope"]["clinical_context_enforced_by_target"])
        self.assertFalse(mapping["treatment_scope"]["dose_enforced_by_target"])
        self.assertEqual(len(mapping["trigger_scope"]["unstructured_clinical_context"]), 3)
        self.assertTrue(any("do not enforce" in value for value in exported["semantic_limits"]))
        self.assertEqual(report["gaps"], [])

    def test_trial_unsupported_or_molecular_conditions_do_not_get_dropped(self):
        for condition in ("baseline PD-L1 expression positive", "co-occurring EGFR V2A mutation",
                          "previously received TEST V1A mutation treatment", "ctDNA clearance at baseline",
                          "70.0% had previously received both platinum-based chemotherapy"):
            with self.subTest(condition=condition):
                self.make_trial_record()
                self.record["fact"]["conditions"].append(condition)
                mapping, _, exported = self.result()
                self.assertEqual(mapping["trigger_bindings"], [])
                self.assertFalse(exported["offline_ready"])

    def test_ctdna_result_blocks_single_mutation_even_when_fact_conditions_omit_it(self):
        self.make_trial_record()
        self.record["fact"]["results"].append({"measure": "ctDNA clearance", "group": "detectable vs undetectable", "value": "reported"})
        mapping, report, exported = self.result()
        self.assertIn("dynamic_ctdna_or_vaf_requires_review", report["gaps"])
        self.assertEqual(mapping["trigger_bindings"], [])
        self.assertFalse(exported["offline_ready"])

    def test_result_subgroup_molecular_conditions_cannot_be_silently_discarded(self):
        for group in ("PD-L1 positive patients", "patients with co-occurring STK11 mutation",
                      "patients with TEST V2A mutation", "patients with high tumor mutational burden"):
            with self.subTest(group=group):
                self.make_trial_record()
                self.record["fact"]["results"][0]["group"] = group
                mapping, report, exported = self.result()
                self.assertIn("additional_result_molecular_scope_requires_review", report["gaps"])
                self.assertEqual(mapping["trigger_bindings"], [])
                self.assertFalse(exported["offline_ready"])

    def test_trial_dose_and_disease_alias_need_matching_original_source(self):
        self.make_trial_record()
        self.record["fact"]["treatment"] = "Drug A, administered orally at a dose of 240 mg once daily"
        self.assertFalse(self.result()[2]["offline_ready"])
        self.make_trial_record()
        self.record["anchors"][0]["quote"] = "A study discussed NSCLC."
        self.record["anchors"][0]["end"] = len(self.record["anchors"][0]["quote"])
        self.assertFalse(self.result()[2]["offline_ready"])

    def test_trial_source_sentence_cannot_hide_a_prior_combination(self):
        self.make_trial_record()
        trial = self.record["anchors"][1]
        trial["quote"] = "Drug B was added; " + trial["quote"]
        trial["end"] = len(trial["quote"])
        self.assertFalse(self.result()[2]["offline_ready"])

    def test_real_source_sentence_with_compact_hgvs_and_receiving_treatment(self):
        # Literal wording from the CC BY article PMID 40445863 body/p:3;
        # IDs and saved fact in this unit test are synthetic.
        quote = ("This was a prospective, translational study conducted on patients with advanced, "
                 "KRASp.G12C–mutant NSCLC who were receiving sotorasib treatment in a real‐world clinical setting.")
        self.replace_quote(quote)
        self.record["fact"].update(population="advanced KRASp.G12C-mutant NSCLC", conditions=["KRASp.G12C mutation"], treatment="sotorasib")
        self.record["entities"] = [{"ref": "drug", "kind": "drug", "label": "sotorasib"},
                                   {"ref": "cancer", "kind": "disease", "label": "NSCLC"},
                                   {"ref": "variant", "kind": "variant", "label": "KRASp.G12C"}]
        self.dictionary["drugs"][0]["name"] = "sotorasib"
        self.dictionary["tumors"][0]["name"] = "NSCLC"
        self.dictionary["logical_targets"] = []
        self.gene_standard["symbols"]["KRAS"] = {"symbol": "KRAS", "gene_id": "3845", "hgnc_id": "6407"}
        mapping, report, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(mapping["trigger_bindings"][0]["input"]["target_key"], "G12C")
        self.assertIn("snapshot_not_covering_key", report["gaps"])
        self.assertEqual(exported["matching_validation"][0]["snapshot_consistency"], "not_covered")
        self.assertEqual(exported["biomed_companion"]["anchors"][0]["quote"], quote)

    def test_separate_gene_and_allele_refs_must_both_be_identified(self):
        self.record["entities"][-1]["label"] = "V1A"
        self.record["entities"].append({"ref": "gene", "kind": "gene", "label": "TEST"})
        self.record["fact"]["entity_roles"]["gene"] = ["molecular"]
        mapping, _, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertCountEqual(mapping["trigger_bindings"][0]["entity_refs"], ["gene", "variant"])

    def test_background_gene_cooccurrence_and_absent_source_do_not_prove_eligibility(self):
        for quote in (
            "Patients with Cancer X were treated with Drug A. TEST V1A mutation was detected in another cohort.",
            "Patients with Cancer X without TEST V1A mutation were treated with Drug A.",
            "Patients with Cancer X harboring TEST V1A mutation were treated with Drug A and Drug B.",
            "Patients with Cancer X harboring TEST V1A mutation were previously treated with Drug A.",
            "Original fixture quote",
        ):
            with self.subTest(quote=quote):
                self.replace_quote(quote)
                mapping, report, exported = self.result()
                self.assertFalse(exported["offline_ready"])
                self.assertEqual(mapping["trigger_bindings"], [])
                self.assertIn("source_does_not_prove_variant_selected_treatment", report["gaps"])

    def test_expression_generic_mutation_and_family_are_not_specific_variant_triggers(self):
        for label, condition in (("TEST", "TEST expression"), ("TEST", "TEST mutation"),
                                 ("H3 K27M", "H3 K27M mutation")):
            with self.subTest(label=label):
                self.record["entities"][-1]["label"] = label
                self.record["fact"]["conditions"] = [condition]
                self.record["fact"]["population"] = "Cancer X " + condition
                mapping, _, exported = self.result()
                self.assertFalse(exported["offline_ready"])
                self.assertEqual(mapping["trigger_bindings"], [])

        self.replace_quote("Patients with Cancer X harboring H3 K27M mutation were treated with Drug A.")
        mapping, report, exported = self.result()
        self.assertFalse(exported["offline_ready"])
        self.assertEqual(mapping["trigger_bindings"], [])
        self.assertIn("gene_symbol_not_confirmed_in_local_standard", report["gaps"])

    def test_compound_conditions_and_unclassified_molecular_roles_are_not_flattened(self):
        for logic in ("and", "or", "complex"):
            self.record["fact"]["molecular_logic"] = logic
            mapping, report, exported = self.result()
            self.assertFalse(exported["offline_ready"])
            self.assertIn("compound_condition_logic_requires_review", report["gaps"])
            self.assertEqual(mapping["trigger_bindings"], [])
        self.record["fact"].pop("molecular_logic")
        self.record["entities"].append({"ref": "other", "kind": "gene", "label": "OTHER"})
        self.record["fact"]["entity_roles"]["other"] = ["molecular"]
        _, report, exported = self.result()
        self.assertFalse(exported["offline_ready"])
        self.assertIn("additional_molecular_roles_need_classification", report["gaps"])

    def test_unextracted_second_variant_in_fact_is_not_discarded(self):
        self.record["fact"]["population"] += " and TEST V2A mutation"
        mapping, report, exported = self.result()
        self.assertFalse(exported["offline_ready"])
        self.assertEqual(mapping["trigger_bindings"], [])
        self.assertIn("additional_fact_conditions_require_review", report["gaps"])

    def test_other_treatment_or_comparator_is_not_promoted_to_current_single_drug(self):
        self.record["entities"].append({"ref": "other", "kind": "drug", "label": "Drug B"})
        self.record["fact"]["entity_roles"]["other"] = ["intervention"]
        for treatment, expected in (("Drug A followed by Drug B", "sequential"),
                                    ("Drug A or Drug B", "alternatives"),
                                    ("Drug A plus Drug B", "combination")):
            self.record["fact"]["treatment"] = treatment
            self.replace_quote("Patients received " + treatment + ".")
            mapping, _, exported = self.result()
            self.assertEqual(mapping["treatment_scope"]["kind"], expected)
            self.assertFalse(exported["offline_ready"])
            if expected in {"alternatives", "sequential"}:
                self.assertEqual(exported["target_payload"]["conclusion_inputs"][0]["drug_ids"], [])
        self.record["fact"]["entity_roles"]["other"] = ["comparator"]
        self.record["fact"]["treatment"] = "Drug A"
        self.replace_quote(self.quote)
        mapping, _, exported = self.result()
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(mapping["treatment_scope"]["entity_refs"], ["drug"])

    def registry(self):
        return {"schema_version": REGISTRY_SCHEMA, "source": {"read_only": True},
                "entities": [{"id": LOCAL_DRUG, "kind": "drug", "status": "active", "name": "Canonical drug",
                              "provenance": {"entity_level": "drug_ingredient"},
                              "external_ids": {"tumor_api": DRUG}},
                             {"id": LOCAL_MAIN, "kind": "disease", "status": "active", "level": "main", "name": "Main cancer",
                              "external_ids": {"tumor_api": TUMOR}},
                             {"id": LOCAL_SUBTYPE, "kind": "disease", "status": "active", "level": "subtype", "name": "Subtype",
                              "main_id": LOCAL_MAIN.replace("-", ""), "external_ids": {"tumor_api": SUBTYPE}}],
                "rules": [{"id": "drug-rule", "kind": "drug", "alias": "Drug A", "scope": {"type": "global", "key": ""},
                           "canonical_id": LOCAL_DRUG.replace("-", ""), "status": "confirmed", "action": "alias"},
                          {"id": "cancer-rule", "kind": "disease", "alias": "Cancer X", "scope": {"type": "article", "key": VERSION.replace("-", "")},
                           "canonical_id": LOCAL_SUBTYPE, "status": "confirmed", "action": "alias"}]}

    def test_confirmed_local_rules_use_target_uuid_and_main_cancer(self):
        registry = self.registry()
        mapping, report, exported = self.result(registry)
        self.assertTrue(exported["offline_ready"])
        self.assertEqual(mapping["entity_ids"], {"drug": DRUG, "cancer": TUMOR})
        self.assertEqual(report["entity_mappings"][1]["local_entity_id"], LOCAL_MAIN)

    def test_unresolved_article_scoped_rule_blocks_global_or_unique_snapshot_fallback(self):
        registry = self.registry()
        registry["rules"][1].update(status="unresolved", canonical_id=None)
        mapping, report, exported = self.result(registry)
        self.assertIn("cancer", mapping["unresolved_entity_refs"])
        self.assertNotIn("cancer", mapping["entity_ids"])
        self.assertEqual(report["entity_mappings"][1]["status"], "local_rule_unresolved")
        self.assertNotIn("cancer", exported["resolved_entities"])
        self.assertFalse(exported["offline_ready"])

    def test_source_stage_qualifier_maps_main_cancer_without_removing_it(self):
        self.record["entities"][1]["label"] = "advanced Cancer X"
        self.record["fact"]["population"] = "advanced Cancer X harboring TEST V1A mutation"
        self.replace_quote("Patients with advanced Cancer X harboring TEST V1A mutation were treated with Drug A.")
        mapping, report, exported = self.result()
        self.assertEqual(mapping["entity_ids"]["cancer"], TUMOR)
        disease = report["entity_mappings"][1]
        self.assertEqual(disease["status"], "source_explicit_qualified_target_name")
        self.assertEqual(disease["source_qualifier"], "advanced")
        self.assertIn("advanced Cancer X", disease["source_support"][0]["quote"])
        self.assertIn("advanced Cancer X", exported["target_payload"]["evidence_description"])
        self.assertTrue(exported["offline_ready"])

    def test_stage_projection_cannot_bypass_unresolved_base_identity(self):
        registry = self.registry()
        registry["rules"][1].update(scope={"type": "global", "key": ""}, status="unresolved", canonical_id=None)
        self.record["entities"][1]["label"] = "advanced Cancer X"
        self.record["fact"]["population"] = "advanced Cancer X harboring TEST V1A mutation"
        self.replace_quote("Patients with advanced Cancer X harboring TEST V1A mutation were treated with Drug A.")
        mapping, _, exported = self.result(registry)
        self.assertNotIn("cancer", mapping["entity_ids"])
        self.assertIn("cancer", mapping["unresolved_entity_refs"])
        self.assertFalse(exported["offline_ready"])

    def test_typographic_hyphens_are_equivalent_only_without_changing_words(self):
        self.dictionary["tumors"][0]["name"] = "non-small cell lung cancer"
        disease = "non‐small cell lung cancer"
        self.record["entities"][1]["label"] = disease
        self.record["fact"]["population"] = disease + " harboring TEST V1A mutation"
        self.replace_quote("Patients with " + disease + " harboring TEST V1A mutation were treated with Drug A.")
        mapping, _, exported = self.result()
        self.assertEqual(mapping["entity_ids"]["cancer"], TUMOR)
        self.assertTrue(exported["offline_ready"])

    def test_qualified_parenthetical_requires_same_unique_target_identity(self):
        self.record["entities"][1]["label"] = "advanced Cancer X (CX)"
        self.record["fact"]["population"] = "advanced Cancer X (CX) harboring TEST V1A mutation"
        self.replace_quote("Patients with advanced Cancer X (CX) harboring TEST V1A mutation were treated with Drug A.")
        self.dictionary["tumors"][0]["aliases"] = ["CX"]
        mapping, _, exported = self.result()
        self.assertEqual(mapping["entity_ids"]["cancer"], TUMOR)
        self.assertTrue(exported["offline_ready"])
        self.dictionary["tumors"].append({"id": SUBTYPE, "name": "Different cancer", "aliases": ["CX"]})
        mapping, _, exported = self.result()
        self.assertNotIn("cancer", mapping["entity_ids"])
        self.assertFalse(exported["offline_ready"])

    def test_dynamic_ctdna_condition_is_reported_and_never_collapsed_to_mutation(self):
        self.record["fact"]["conditions"].append("clearance of KRAS G12C-mutant ctDNA levels")
        mapping, report, exported = self.result()
        self.assertIn("dynamic_ctdna_or_vaf_requires_review", report["gaps"])
        self.assertEqual(mapping["trigger_bindings"], [])
        self.assertFalse(exported["offline_ready"])

    def test_confirmed_local_drug_is_an_automatic_prerequisite_without_fake_target_uuid(self):
        registry = self.registry()
        registry["entities"][0]["external_ids"] = {"RXNORM": "123"}
        self.dictionary["drugs"] = []
        mapping, report, exported = self.result(registry)
        self.assertEqual(report["entity_mappings"][0]["status"], "automatic_target_upsert")
        self.assertNotIn("drug", mapping["entity_ids"])
        self.assertNotIn("drug", mapping["unresolved_entity_refs"])
        self.assertFalse(exported["offline_ready"])
        self.assertTrue(exported["entity_sync_ready"])
        self.assertEqual(exported["compatibility_status"], "ready_after_entity_sync")
        self.assertEqual(exported["mapping_issues"], [])
        operation = exported["entity_operations"][0]
        self.assertFalse(operation["requires_human_approval"])
        self.assertEqual(operation["create_payload"]["name_cn"], "Canonical drug")
        self.assertEqual(exported["target_payload"]["conclusion_inputs"][0]["drug_ids"], [])
        self.assertEqual(exported["target_payload_template"]["conclusion_inputs"][0]["drug_ids"],
                         [{"$entity_operation": operation["operation_id"]}])

    def test_confirmed_drug_class_is_not_an_ingredient_addition_request(self):
        registry = self.registry()
        registry["entities"][0]["external_ids"] = {}
        registry["entities"][0]["provenance"]["entity_level"] = "drug_class"
        _, report, exported = self.result(registry)
        self.assertEqual(report["entity_mappings"][0]["status"], "target_class_identity_missing")
        self.assertEqual([e["code"] for e in exported["mapping_issues"] if e["path"] == "drug"], ["target_class_identity_missing"])

    def test_unknown_identity_has_one_specific_issue_without_fake_local_conflict(self):
        self.dictionary["drugs"] = []
        _, report, exported = self.result()
        self.assertEqual(report["entity_mappings"][0]["status"], "target_identity_missing")
        self.assertEqual([e["code"] for e in exported["mapping_issues"] if e["path"] == "drug"], ["target_identity_missing"])

    def test_source_qualified_abbreviation_maps_only_explicit_same_article_definition(self):
        label = "locally advanced or metastatic Cancer X"
        quote = f"Patients with {label} (la/mUC) harboring TEST V1A mutation were treated with Drug A."
        self.record["entities"][1]["label"] = label
        self.record["entities"].append({"ref": "abbr", "kind": "disease", "label": "la/mUC"})
        self.record["fact"]["entity_roles"]["abbr"] = ["disease"]
        self.record["fact"]["population"] = f"{label} (la/mUC) harboring TEST V1A mutation"
        self.replace_quote(quote)
        mapping, report, exported = self.result()
        self.assertEqual(mapping["entity_ids"]["cancer"], TUMOR)
        self.assertEqual(mapping["entity_ids"]["abbr"], TUMOR)
        alias = next(e for e in report["entity_mappings"] if e["entity_ref"] == "abbr")
        self.assertEqual(alias["status"], "source_defined_qualified_abbreviation")
        self.assertEqual(alias["source_qualifier"], "locally advanced or metastatic")
        self.assertEqual(alias["definition_scope"], {"type": "article", "version_id": VERSION})
        self.assertIn(f"{label} (la/mUC)", alias["source_support"][0]["quote"])
        self.assertEqual(exported["biomed_companion"], self.record)
        # Two disease refs and source fact logic remain independently assessed.
        self.assertFalse(exported["offline_ready"])

    def test_context_can_prove_alias_identity_but_not_treatment_outcome(self):
        self.record["entities"][1]["label"] = "la/mUC"
        self.record["fact"]["population"] = "la/mUC harboring TEST V1A mutation"
        self.replace_quote("Overall response was reported for Drug A.")
        definition = "Patients with locally advanced or metastatic Cancer X (la/mUC) were studied."
        self.record["anchors"].append({"purpose": "context", "passage_id": "99999999-9999-4999-8999-999999999999",
                                       "start": 0, "end": len(definition), "quote": definition})
        mapping, report, exported = self.result()
        self.assertEqual(mapping["entity_ids"]["cancer"], TUMOR)
        self.assertEqual(report["entity_mappings"][1]["source_support"][0]["purpose"], "context")
        self.assertFalse(exported["offline_ready"])
        self.assertIn("source_does_not_prove_variant_selected_treatment", report["gaps"])

    def test_scoped_abbreviation_without_definition_or_with_negation_stays_unresolved(self):
        self.record["entities"][1]["label"] = "la/mUC"
        self.record["fact"]["population"] = "la/mUC harboring TEST V1A mutation"
        for quote in ("Patients with la/mUC harboring TEST V1A mutation were treated with Drug A.",
                      "Patients without locally advanced or metastatic Cancer X (la/mUC) were treated with Drug A.",
                      "Patients with locally advanced or metastatic Cancer X and Cancer Y (la/mUC) were treated with Drug A."):
            self.replace_quote(quote)
            mapping, _, exported = self.result()
            self.assertNotIn("cancer", mapping["entity_ids"])
            self.assertFalse(exported["offline_ready"])

    def test_rejected_scoped_abbreviation_blocks_global_and_source_fallback(self):
        registry = self.registry()
        registry["rules"].append({"id": "rejected", "kind": "disease", "alias": "la/mUC",
                                  "scope": {"type": "article", "key": VERSION}, "canonical_id": None,
                                  "status": "rejected", "action": "alias"})
        self.record["entities"][1]["label"] = "la/mUC"
        self.record["fact"]["population"] = "la/mUC harboring TEST V1A mutation"
        self.replace_quote("Patients with locally advanced or metastatic Cancer X (la/mUC) harboring TEST V1A mutation were treated with Drug A.")
        mapping, report, _ = self.result(registry)
        self.assertNotIn("cancer", mapping["entity_ids"])
        self.assertEqual(report["entity_mappings"][1]["status"], "local_rule_unresolved")

    def test_negated_full_qualified_name_is_not_mapped(self):
        label = "locally advanced or metastatic Cancer X"
        self.record["entities"][1]["label"] = label
        self.record["fact"]["population"] = "without " + label
        self.replace_quote(f"Patients without {label} were treated with Drug A.")
        mapping, _, exported = self.result()
        self.assertNotIn("cancer", mapping["entity_ids"])
        self.assertFalse(exported["offline_ready"])

    def test_cli_writes_immutable_mapping_report_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in (("bundle", self.bundle), ("dictionary", self.dictionary)):
                (root / f"{name}.json").write_text(json.dumps(value))
            (root / "genes.jsonl").write_text(json.dumps({"symbol": "TEST", "gene_id": 1, "hgnc_id": "1", "type": "protein-coding"}) + "\n")
            cmd = [sys.executable, "scripts/prepare_knowledge_target_mapping.py", "--bundle", str(root / "bundle.json"),
                   "--dictionary", str(root / "dictionary.json"), "--output", str(root / "mapping.json"),
                   "--gene-standard", str(root / "genes.jsonl"), "--report", str(root / "report.json"),
                   "--export", str(root / "export.json")]
            first = subprocess.run(cmd, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(first.stdout)["offline_ready_count"], 1)
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads((root / "export.json").read_text())["offline_ready_count"], 1)
            (root / "report.json").write_text("different")
            failure = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("Refusing to overwrite", failure.stderr)


if __name__ == "__main__":
    unittest.main()
