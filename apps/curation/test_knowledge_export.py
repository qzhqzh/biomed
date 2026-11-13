import copy
import unittest

from apps.curation.knowledge_export import export_bundle, validate_target_payload

DRUG = "11111111-1111-4111-8111-111111111111"
TUMOR = "22222222-2222-4222-8222-222222222222"
RULE = "33333333-3333-4333-8333-333333333333"


class KnowledgeExportTests(unittest.TestCase):
    def setUp(self):
        # Synthetic IDs are isolated test fixtures, never a production mapping.
        self.dictionary = {
            "schema_version": "tumor-api-dictionary-v1",
            "source": {"snapshot_id": "synthetic-contract-test", "fixture": True},
            "drugs": [{"id": DRUG, "name": "Drug A", "aliases": []}],
            "tumors": [{"id": TUMOR, "name": "Cancer X", "aliases": []}],
            "rules": [{"id": RULE, "review_status": "approved", "is_active": True, "is_locked": False}],
            "logical_targets": [{"event_type": "mutation", "gene_symbol": "TEST", "target_key": "V1A", "source_table": "synthetic"}],
        }
        self.payload = {
            "evidence_description": "Synthetic treatment evidence",
            "evidence_category": "treatment",
            "conclusion_inputs": [{"potency": "-", "drug_ids": [DRUG],
                                   "disease_scope_inputs": [{"tumor_id": TUMOR}],
                                   "trigger_inputs": [{"trigger_type": "direct", "event_type": "mutation",
                                                       "gene_symbol": "TEST", "target_key": "p.V1A"}]}],
        }
        self.bundle = {
            "schema_version": "knowledge-candidates-v1", "source_run": "test-export-v1", "records": [{
                "group_id": "K01", "version_id": "55555555-5555-4555-8555-555555555555", "source_sha256": "a" * 64,
                "article": {"pmid": "fixture", "title": "Synthetic source"},
                "fact": {"relation": "treatment_outcome", "population": "Cancer X", "treatment": "Drug A",
                         "conclusion": "Drug A was associated with benefit in Cancer X",
                         "drug_names": ["Drug A"], "direction": "benefit", "scope": "human",
                         "conditions": ["TEST p.V1A"],
                         "entity_roles": {"e1": ["intervention"], "e2": ["disease"], "e3": ["molecular"]}},
                "entities": [{"ref": "e1", "kind": "drug", "label": "Drug A"},
                             {"ref": "e2", "kind": "disease", "label": "Cancer X"},
                             {"ref": "e3", "kind": "variant", "label": "TEST p.V1A"}],
                "anchors": [{"purpose": "evidence", "passage_id": "66666666-6666-4666-8666-666666666666",
                             "start": 0, "end": 22, "quote": "Original fixture quote"}],
                "assessment": {"quality": {"status": "not_reviewed"}},
            }],
        }
        self.mappings = {"K01": {"trigger_bindings": [{
            "entity_refs": ["e3"], "basis": "synthetic test normalization",
            "input": self.payload["conclusion_inputs"][0]["trigger_inputs"][0],
        }]}}
        support = [{"passage_id": self.bundle["records"][0]["anchors"][0]["passage_id"], "quote": "Original fixture quote"}]
        self.mappings["K01"].update({
            "trigger_scope": {"kind": "direct", "condition_entity_refs": ["e3"], "non_trigger_entity_refs": [],
                              "logic": "single", "source_support": support},
            "treatment_scope": {"kind": "single", "entity_refs": ["e1"], "groups": [], "source_support": support},
        })

    def test_positive_offline_export_preserves_unreviewed_knowledge_and_sources(self):
        original = copy.deepcopy(self.bundle)
        package = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)
        row = package["records"][0]
        self.assertEqual(package["schema_version"], "biomed.tumor-api-export.v1")
        self.assertEqual(package["contract"]["version"], "biomed.target-contract-assessment.v4")
        self.assertEqual(row["compatibility_status"], "offline_validated")
        self.assertTrue(row["contract_validation"]["valid"])
        self.assertEqual(package["offline_ready_count"], 1)
        self.assertFalse(package["submit_ready"])
        self.assertFalse(package["target_contacted"])
        self.assertEqual(row["quality"]["status"], "not_reviewed")
        self.assertEqual(row["biomed_companion"], original["records"][0])
        self.assertEqual(self.bundle, original)
        conclusion = row["target_payload"]["conclusion_inputs"][0]
        self.assertEqual(conclusion["drug_ids"], [DRUG])
        self.assertEqual(conclusion["potency"], "-")  # Benefit is never invented sensitivity.
        self.assertEqual(row["target_payload"]["evidence_level"], "-")
        self.assertFalse(row["target_payload"]["is_active"])
        self.assertEqual(row["matching_validation"][0]["snapshot_consistency"], "consistent")
        self.assertEqual(row["live_sample_matching"], "unverified")
        self.assertIn(original["records"][0]["fact"]["conclusion"], row["target_payload"]["evidence_description"])

    def test_missing_snapshot_never_treats_uuid_as_resolved(self):
        result = validate_target_payload(self.payload)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["unresolved"]), 2)
        self.assertEqual(result["errors"], [])
        row = export_bundle(self.bundle, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "pending_mapping")

    def test_target_ids_are_not_ontology_or_biomed_ids(self):
        self.payload["conclusion_inputs"][0]["drug_ids"] = ["RXNORM:123"]
        result = validate_target_payload(self.payload, self.dictionary)
        self.assertIn("invalid_target_uuid", {e["code"] for e in result["errors"]})

    def test_triggerless_knowledge_is_preserved_but_not_representable(self):
        row = self.bundle["records"][0]
        del row["fact"]["entity_roles"]["e3"]
        package = export_bundle(self.bundle, dictionary=self.dictionary)
        result = package["records"][0]
        self.assertEqual(result["compatibility_status"], "not_representable")
        self.assertEqual(result["biomed_companion"], row)
        self.assertEqual(result["target_payload"]["conclusion_inputs"][0]["trigger_inputs"], [])

    def test_expression_and_invalid_trigger_fields_are_rejected(self):
        trigger = self.payload["conclusion_inputs"][0]["trigger_inputs"][0]
        trigger["event_type"] = "protein_expression"
        self.assertFalse(validate_target_payload(self.payload, self.dictionary)["valid"])
        trigger["event_type"] = "mutation"
        trigger["target_rule_id"] = RULE
        self.assertFalse(validate_target_payload(self.payload, self.dictionary)["valid"])

    def test_unresolved_scope_without_extracted_molecular_entities_stays_pending(self):
        source = self.bundle["records"][0]
        source["fact"]["entity_roles"].pop("e3")
        self.mappings["K01"]["trigger_scope"].update(
            kind="unresolved", condition_entity_refs=[], non_trigger_entity_refs=[])
        self.mappings["K01"]["trigger_bindings"] = []
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "pending_mapping")
        self.assertFalse(row["offline_ready"])
        self.assertEqual(row["biomed_companion"], source)

    def test_rule_requires_snapshot_and_approved_active_unlocked_state(self):
        self.payload["conclusion_inputs"][0]["trigger_inputs"] = [{"trigger_type": "rule", "target_rule_id": RULE}]
        self.assertTrue(validate_target_payload(self.payload, self.dictionary)["valid"])
        for key, bad in (("review_status", "pending"), ("is_active", False), ("is_locked", True)):
            dictionary = copy.deepcopy(self.dictionary)
            dictionary["rules"][0][key] = bad
            errors = validate_target_payload(self.payload, dictionary)["errors"]
            self.assertIn("unusable_rule", {e["code"] for e in errors})

    def test_duplicates_and_non_treatment_drugs_fail(self):
        conclusion = self.payload["conclusion_inputs"][0]
        conclusion["drug_ids"].append(DRUG)
        self.assertIn("duplicate_items", {e["code"] for e in validate_target_payload(self.payload, self.dictionary)["errors"]})
        conclusion["drug_ids"] = [DRUG]
        self.payload["conclusion_inputs"].append(copy.deepcopy(conclusion))
        self.payload["evidence_category"] = "diagnostic"
        codes = {e["code"] for e in validate_target_payload(self.payload, self.dictionary)["errors"]}
        self.assertTrue({"duplicate_conclusion", "treatment_only"} <= codes)

    def test_target_canonicalization_does_not_hide_duplicate_cnv_or_fusion_entries(self):
        for event, values in (("cnv", ["amp", "Amplification"]), ("fusion", ["TEST-OTHER fusion", "TEST::OTHER"])):
            self.payload["conclusion_inputs"][0]["trigger_inputs"] = [
                {"trigger_type": "direct", "event_type": event, "gene_symbol": "TEST", "target_key": value}
                for value in values
            ]
            codes = {e["code"] for e in validate_target_payload(self.payload, self.dictionary)["errors"]}
            self.assertIn("duplicate_items", codes)

    def test_source_and_unknown_input_fields_are_not_silently_dropped(self):
        self.payload["pubmed_id"] = "not-a-target-input"
        self.payload["evidence_source"] = "x" * 513
        codes = {e["code"] for e in validate_target_payload(self.payload, self.dictionary)["errors"]}
        self.assertTrue({"unknown_field", "invalid_string"} <= codes)
        self.bundle["records"][0]["anchors"] = []
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "invalid_source")

    def test_legacy_entity_inventory_is_not_used_as_relationship_participants(self):
        self.bundle["records"][0]["fact"].pop("entity_roles")
        row = export_bundle(self.bundle, dictionary=self.dictionary)["records"][0]
        self.assertEqual(row["target_payload"]["conclusion_inputs"][0]["drug_ids"], [])
        self.assertIn("participant_roles_pending", {e["code"] for e in row["mapping_issues"]})
        self.assertEqual(row["compatibility_status"], "pending_mapping")

    def test_compound_condition_cannot_be_flattened_to_direct_entry(self):
        self.mappings["K01"]["trigger_scope"]["logic"] = "and"
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "pending_mapping")
        self.assertIn("compound_rule_required", {e["code"] for e in row["mapping_issues"]})

    def test_mechanism_and_result_markers_are_not_eligibility_triggers(self):
        source = self.bundle["records"][0]
        source["entities"].append({"ref": "e4", "kind": "gene", "label": "PARP cleavage"})
        source["fact"]["entity_roles"]["e4"] = ["molecular"]
        self.mappings["K01"]["trigger_scope"]["non_trigger_entity_refs"] = ["e4"]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertTrue(row["offline_ready"])
        self.assertEqual(len(row["target_payload"]["conclusion_inputs"][0]["trigger_inputs"]), 1)

    def test_generic_mutation_or_wrong_allele_is_not_an_existing_target_match(self):
        binding = self.mappings["K01"]["trigger_bindings"][0]
        for key in ("mutation", "V2A"):
            binding["input"]["target_key"] = key
            self.dictionary["logical_targets"].append({"event_type": "mutation", "gene_symbol": "TEST", "target_key": key})
            row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
            self.assertTrue(row["contract_validation"]["valid"])
            self.assertFalse(row["offline_ready"])
            self.assertEqual(row["matching_validation"][0]["snapshot_consistency"], "unverified")

    def test_h3_family_is_not_narrowed_to_a_specific_gene_or_alias(self):
        self.bundle["records"][0]["entities"][2]["label"] = "H3 K27M"
        self.mappings["K01"]["trigger_bindings"][0]["input"].update(gene_symbol="H3F3A", target_key="K27M")
        self.dictionary["logical_targets"] = [{"event_type": "mutation", "gene_symbol": "H3F3A", "target_key": "K27M"}]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertIn("source_condition_does_not_identify_this_gene", row["matching_validation"][0]["issues"])
        self.assertFalse(row["offline_ready"])

    def test_single_source_variant_cannot_expand_to_multiple_target_keys(self):
        self.bundle["records"][0]["entities"][2]["label"] = "H3-3A K27M"
        self.mappings["K01"]["trigger_bindings"][0]["input"].update(
            gene_symbol="H3-3A", target_key="K27M,K28M,K27I")
        self.dictionary["logical_targets"] = [
            {"event_type": "mutation", "gene_symbol": "H3-3A", "target_key": "K27M,K28M,K27I"}]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertTrue(row["contract_validation"]["valid"])
        check = row["matching_validation"][0]
        self.assertTrue(check["snapshot_matches"])
        self.assertIn("source_condition_does_not_identify_this_variant_key", check["issues"])
        self.assertEqual(check["snapshot_consistency"], "unverified")
        self.assertFalse(row["offline_ready"])

    def test_rule_basis_does_not_substitute_for_matching_ast_and_conditions(self):
        source = self.bundle["records"][0]
        source["entities"].append({"ref": "e4", "kind": "variant", "label": "TEST V2A"})
        source["fact"]["entity_roles"]["e4"] = ["molecular"]
        scope = self.mappings["K01"]["trigger_scope"]
        scope.update(kind="rule", condition_entity_refs=["e3", "e4"], logic="and")
        conditions = [{"code": code, "event_type": "mutation", "gene_symbol": "TEST", "target_key": key}
                      for code, key in (("A", "V1A"), ("B", "V2A"))]
        ast = {"op": "all", "children": [{"condition": "A"}, {"condition": "B"}]}
        self.dictionary["rules"][0].update(expression=ast, conditions=conditions)
        binding = {"entity_refs": ["e3", "e4"], "basis": "Documented interpretation, insufficient without AST",
                   "input": {"trigger_type": "rule", "target_rule_id": RULE}}
        self.mappings["K01"]["trigger_bindings"] = [binding]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertFalse(row["offline_ready"])
        binding.update(expected_expression=copy.deepcopy(ast), expected_conditions=conditions)
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertTrue(row["offline_ready"])
        binding["expected_expression"]["op"] = "any"
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertIn("source_condition_ast_differs_from_target_rule", row["matching_validation"][0]["issues"])
        self.assertFalse(row["offline_ready"])

    def test_alternative_drugs_are_not_flattened_and_mapping_is_retained(self):
        other = "44444444-4444-4444-8444-444444444444"
        self.dictionary["drugs"].append({"id": other, "name": "Drug B"})
        self.bundle["records"][0]["entities"].append({"ref": "e4", "kind": "drug", "label": "Drug B"})
        self.bundle["records"][0]["fact"]["entity_roles"]["e4"] = ["intervention"]
        scope = self.mappings["K01"]["treatment_scope"]
        scope.update(kind="alternatives", entity_refs=["e1", "e4"], groups=[{"entity_refs": ["e1"]}, {"entity_refs": ["e4"]}])
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["target_payload"]["conclusion_inputs"][0]["drug_ids"], [])
        self.assertEqual(row["adaptation"]["treatment_scope"], scope)
        self.assertEqual(row["resolved_entities"]["e4"]["target_id"], other)
        self.assertIn("treatment_requires_split", {e["code"] for e in row["mapping_issues"]})
        self.assertFalse(row["offline_ready"])

    def test_mapping_cannot_promote_comparator_or_background_entities(self):
        source = self.bundle["records"][0]
        source["fact"]["entity_roles"]["e1"] = ["comparator"]
        source["fact"]["entity_roles"]["e3"] = ["context"]
        original = copy.deepcopy(source)
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        codes = {e["code"] for e in row["mapping_issues"]}
        self.assertTrue({"invalid_treatment_scope", "invalid_trigger_scope"} <= codes)
        conclusion = row["target_payload"]["conclusion_inputs"][0]
        self.assertEqual(conclusion["drug_ids"], [])
        self.assertEqual(conclusion["trigger_inputs"], [])
        self.assertFalse(row["offline_ready"])
        self.assertEqual(row["biomed_companion"], original)

    def test_regimen_and_prior_drug_names_are_diagnostics_not_required_interventions(self):
        self.bundle["records"][0]["fact"]["drug_names"] += ["Drug A 10 mg", "Prior Drug"]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertTrue(row["offline_ready"])
        self.assertEqual(len(row["named_term_diagnostics"]), 3)
        self.assertEqual(row["target_payload"]["conclusion_inputs"][0]["drug_ids"], [DRUG])

    def test_expression_condition_and_unanchored_interpretation_are_not_ready(self):
        scope = self.mappings["K01"]["trigger_scope"]
        scope.update(kind="unsupported_marker")
        self.mappings["K01"]["trigger_bindings"] = []
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "not_representable")
        self.assertIn("unsupported_biomarker_condition", {e["code"] for e in row["mapping_issues"]})
        scope.update(kind="direct", source_support=[{"passage_id": "not-in-record", "quote": "invented"}])
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertIn("trigger_scope_source_unverified", {e["code"] for e in row["mapping_issues"]})

    def test_partial_snapshot_cannot_prove_a_logical_target_does_not_exist(self):
        self.dictionary["logical_targets"] = []
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "pending_mapping")
        self.assertIn("new_key_gene_standard_provenance_required", row["matching_validation"][0]["issues"])

    def test_new_concrete_key_is_structurally_valid_but_snapshot_and_sample_remain_unverified(self):
        self.dictionary["logical_targets"] = []
        source = self.bundle["records"][0]
        quote = "Patients with Cancer X harboring TEST V1A mutation were treated with Drug A."
        anchor = source["anchors"][0]
        anchor.update(quote=quote, end=len(quote))
        support = [{"passage_id": anchor["passage_id"], "quote": quote}]
        self.mappings["K01"]["trigger_bindings"][0]["source_support"] = support
        self.mappings["K01"]["trigger_bindings"][0]["gene_standard_identity"] = {"symbol": "TEST", "gene_id": "1", "hgnc_id": "2"}
        self.mappings["K01"]["trigger_bindings"][0]["gene_standard_source"] = {"sha256": "c" * 64}
        self.mappings["K01"]["trigger_scope"]["source_support"] = support
        self.mappings["K01"]["treatment_scope"]["source_support"] = support
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertTrue(row["offline_ready"])
        self.assertEqual(row["matching_validation"][0]["snapshot_consistency"], "not_covered")
        self.assertIn("snapshot_not_covering_key", row["matching_validation"][0]["warnings"])
        self.assertEqual(row["live_sample_matching"], "unverified")
        self.assertFalse(row["submit_ready"])

    def test_new_unconfirmed_family_name_cannot_be_a_gene_symbol(self):
        self.dictionary["logical_targets"] = []
        source = self.bundle["records"][0]
        source["entities"][2]["label"] = "H3 K27M"
        quote = "Patients with Cancer X harboring H3 K27M mutation were treated with Drug A."
        anchor = source["anchors"][0]
        anchor.update(quote=quote, end=len(quote))
        support = [{"passage_id": anchor["passage_id"], "quote": quote}]
        self.mappings["K01"]["trigger_bindings"][0].update(
            input={"trigger_type": "direct", "event_type": "mutation", "gene_symbol": "H3", "target_key": "K27M"},
            source_support=support)
        self.mappings["K01"]["trigger_scope"]["source_support"] = support
        self.mappings["K01"]["treatment_scope"]["source_support"] = support
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertFalse(row["offline_ready"])
        self.assertIn("new_key_gene_standard_provenance_required", row["matching_validation"][0]["issues"])

    def test_local_rule_conflict_prevents_automatic_snapshot_name_fallback(self):
        self.mappings["K01"]["unresolved_entity_refs"] = ["e1"]
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertFalse(row["offline_ready"])
        self.assertNotIn("e1", row["resolved_entities"])
        self.assertIn("local_identity_unresolved", {e["code"] for e in row["mapping_issues"]})

    def test_ambiguous_alias_does_not_choose_first_target(self):
        self.dictionary["drugs"].append({"id": "44444444-4444-4444-8444-444444444444", "name": "Other", "aliases": ["Drug A"]})
        row = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)["records"][0]
        self.assertEqual(row["compatibility_status"], "pending_mapping")
        self.assertEqual(row["target_payload"]["conclusion_inputs"][0]["drug_ids"], [])

    def test_export_is_deterministic_and_does_not_mix_groups(self):
        one = export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings)
        self.assertEqual(one, export_bundle(self.bundle, dictionary=self.dictionary, mappings=self.mappings))
        with self.assertRaisesRegex(ValueError, "unknown group_id"):
            export_bundle(self.bundle, mappings={"K99": {}})


if __name__ == "__main__":
    unittest.main()
