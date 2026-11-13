import copy
import unittest

from apps.curation.knowledge_export import export_bundle
from apps.curation.knowledge_target_readiness import readiness_summary, target_readiness


class TargetReadinessTests(unittest.TestCase):
    def record(self, quote, **fact):
        return {
            "group_id": "synthetic", "version_id": "11111111-1111-4111-8111-111111111111",
            "source_sha256": "a" * 64, "article": {"pmid": "synthetic"},
            "fact": {"relation": "biomarker_association", "population": "Cancer X",
                     "conditions": ["higher TEST expression"], "results": [{"measure": "survival", "value": "associated"}],
                     "entity_roles": {"g": ["molecular"], "c": ["disease"]}, **fact},
            "entities": [{"ref": "g", "kind": "gene", "label": "TEST"}, {"ref": "c", "kind": "disease", "label": "Cancer X"}],
            "assessment": {"execution": {"status": "complete"}, "compatibility": {"status": "pending_mapping"}},
            "anchors": [{"purpose": "evidence", "quote": quote, "start": 0, "end": len(quote),
                         "passage_id": "22222222-2222-4222-8222-222222222222"}],
        }

    def test_expression_is_not_an_id_mapping_task_and_original_knowledge_is_preserved(self):
        row = self.record("Higher TEST expression was associated with shorter survival in Cancer X.")
        original = copy.deepcopy(row)
        result = target_readiness(row)
        self.assertEqual(result["reason_code"], "expression_not_genomic_event")
        bundle = {"schema_version": "knowledge-candidates-v1", "source_run": "synthetic", "records": [row]}
        package = export_bundle(bundle)
        self.assertEqual(package["records"][0]["compatibility_status"], "not_representable")
        self.assertFalse(package["records"][0]["offline_ready"])
        self.assertEqual(package["records"][0]["biomed_companion"], original)
        self.assertEqual(row, original)

    def test_pooled_treatment_statistics_have_no_invented_trigger(self):
        row = self.record("Phase 2 trials had a lower response rate than phase 3 trials.", relation="treatment_outcome",
                          conditions=["pooled across cancers"], entity_roles={"p2": ["intervention"]})
        self.assertEqual(target_readiness(row)["reason_code"], "no_genomic_condition")

    def test_mechanism_and_figure_numbers_are_not_genomic_events(self):
        row = self.record("TEST promotes cell proliferation in vitro (Fig. S1A). Tumor stage (T2a vs. T4) was recorded.",
                          relation="mechanism", conditions=["in vitro signaling axis"])
        self.assertEqual(target_readiness(row)["reason_code"], "mechanism_not_genomic_event")

    def test_a_source_event_missed_by_extraction_stays_open(self):
        for signal in ["TEST G12C mutation", "TEST amplification", "TEST::OTHER fusion", "ctDNA clearance", "p.V600E", "c.35G>T"]:
            with self.subTest(signal=signal):
                row = self.record(f"Higher TEST expression was associated with survival. Patients also had {signal}.")
                self.assertEqual(target_readiness(row)["status"], "mapping_needed")

    def test_missing_or_incomplete_sources_are_not_evidence_of_non_applicability(self):
        row = self.record("Higher TEST expression was associated with survival.")
        row["anchors"][0]["end"] -= 1
        self.assertEqual(target_readiness(row)["status"], "mapping_needed")
        row["anchors"] = []
        self.assertEqual(target_readiness(row)["status"], "mapping_needed")

    def test_existing_validated_or_auto_entity_sync_path_is_not_demoted(self):
        row = self.record("Higher TEST expression was associated with survival.")
        for status, expected in [("offline_validated", "offline_ready"), ("ready_after_entity_sync", "entity_sync"),
                                 ("preview_validated", "preview_only")]:
            self.assertEqual(target_readiness(row, status)["status"], expected)

    def test_denominator_excludes_structure_failures_and_does_not_use_quality(self):
        good = self.record("Higher TEST expression was associated with survival.")
        bad = copy.deepcopy(good)
        bad.update(group_id="failed", fact={})
        summary = readiness_summary([good, bad])
        self.assertEqual(summary["structurally_eligible"], 1)
        self.assertEqual(summary["counts"], {"outside_target": 1, "extraction_gap": 1})
        good["assessment"]["quality"] = {"status": "invalid"}
        self.assertEqual(target_readiness(good)["status"], "outside_target")

    def test_genomic_background_is_conservative_rather_than_silently_removed(self):
        row = self.record("TEST expression regulates signaling. Prior research reported TEST mutations in another cancer.")
        self.assertEqual(target_readiness(row)["reason_code"], "genomic_mapping_unresolved")
