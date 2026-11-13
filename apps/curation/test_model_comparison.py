import json
import tempfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.curation.decision import digest
from apps.curation.model_comparison import evaluate_runs, pairwise_comparisons, score_predictions
from apps.curation.research_history import prepare_record
from apps.curation.three_class_evaluation import file_hash
from scripts.evaluate_model_comparison import public_record


class ModelComparisonScoringTests(SimpleTestCase):
    def setUp(self):
        self.rows = [{"pmid": str(i)} for i in range(4)]
        self.references = {str(i): {"category": category} for i, category in enumerate(
            ("solid", "hematologic", "non_neoplastic", None))}
        self.predictions = {
            "0": {"category": "solid", "status": "classified"},
            "1": {"category": "non_neoplastic", "status": "classified"},
            "2": {"category": None, "status": "invalid_output"},
            "3": {"category": "non_neoplastic", "status": "classified"},
        }

    def test_failures_stay_in_denominator_and_unknown_reference_is_excluded(self):
        result = score_predictions(self.rows, self.references, self.predictions)
        self.assertEqual((result["matched"], result["denominator"]), (1, 3))
        self.assertEqual((result["failed"], result["unscored"]), (1, 1))
        self.assertAlmostEqual(result["macro_recall"], 1 / 3)
        self.assertEqual(result["confusion"]["non_neoplastic"]["failed"], 1)
        self.assertEqual(result["tumor_false_exclusions"], 1)

    def test_unknown_reference_does_not_affect_precision(self):
        self.predictions["3"]["category"] = "solid"
        result = score_predictions(self.rows, self.references, self.predictions)
        self.assertEqual(result["by_class"]["solid"]["precision"], 1)

    def test_abstention_is_unfinished_not_a_correct_non_tumor_prediction(self):
        self.predictions["2"]["status"] = "abstained"
        result = score_predictions(self.rows, self.references, self.predictions)
        self.assertEqual(result["denominator"], 3)
        self.assertEqual(result["abstained"], 1)
        self.assertEqual(result["by_class"]["non_neoplastic"]["recall"], 0)

    def test_missing_and_unexpected_predictions_cannot_shrink_the_denominator(self):
        del self.predictions["1"]
        with self.assertRaisesMessage(ValueError, "Incomplete"):
            score_predictions(self.rows, self.references, self.predictions)
        self.predictions["unexpected"] = {"category": "solid", "status": "classified"}
        with self.assertRaisesMessage(ValueError, "unexpected"):
            score_predictions(self.rows, self.references, self.predictions)

    def test_pairwise_reports_regressions_as_well_as_net_gain(self):
        common = {"denominator": 3, "macro_recall": 0.5, "mean_seconds": 1, "nvml_process_peak_mib": 1000}
        methods = [{**common, "label": "Qwen3-1.7B", "parameters": 1700000000, "matched": 1},
                   {**common, "label": "Qwen3-8B", "parameters": 8000000000, "matched": 2}]
        cases = [{"reference": reference, "predictions": [
            {"label": "Qwen3-1.7B", "outcome": before}, {"label": "Qwen3-8B", "outcome": after}]}
            for reference, before, after in [("solid", "matched", "disagreed"),
                ("solid", "disagreed", "matched"), ("hematologic", "failed", "matched"),
                (None, "disagreed", "matched")]]
        result = pairwise_comparisons(methods, cases)[0]
        self.assertEqual((result["improved"], result["regressed"], result["correct_delta"]), (2, 1, 1))
        self.assertAlmostEqual(result["accuracy_delta_pp"], 100 / 3)

    def test_group_comparison_does_not_invent_resources_or_missing_class_recall(self):
        methods = [{"label": label, "denominator": 0, "matched": 0, "macro_recall": None}
            for label in ("Qwen3-1.7B", "Qwen3-8B")]
        result = pairwise_comparisons(methods, [], include_resources=False)[0]
        self.assertIsNone(result["accuracy_delta_pp"])
        self.assertIsNone(result["macro_recall_delta_pp"])
        self.assertNotIn("memory_ratio", result)


class ExpandedModelComparisonTests(SimpleTestCase):
    """Exercise frozen provenance and independent new-cohort scores end to end."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "expanded"
        self.original = self.root / "original"
        self.run = self.root / "run"
        for folder in (self.base, self.original, self.run):
            folder.mkdir()
        self.definition = {"task_version": "literature-three-class-v1", "boundary_notes": []}
        self.write(self.base / "task-definition-v1.json", self.definition)
        self.write(self.original / "task-definition-v1.json", self.definition)
        self.rows, references = [], []
        for index in range(100):
            category = ("solid", "hematologic", "non_neoplastic")[index % 3] if index < 99 else None
            metadata = {"title": f"Study {index}", "abstract": "", "mesh": [], "publication_types": []}
            row = {"pmid": str(index + 1), "version_id": f"v{index}", "source_sha256": "a" * 64,
                "input_sha256": digest(metadata), "metadata": metadata}
            self.rows.append(row)
            references.append({**{key: row[key] for key in ("pmid", "version_id", "source_sha256", "input_sha256")},
                "category": category, "reference_state": "determinate" if category else "unresolved",
                "reason": "Frozen reference", "evidence": [{"field": "title", "text": metadata["title"],
                    "start": 0, "end": len(metadata["title"])}]})
        self.reference = {"schema_version": "three-class-reference-v1", "annotator_model": "gpt-6-astra",
            "status": "model_reference_not_human_gold", "blinded_to_candidates": True,
            "input_scope": "title_abstract_mesh_publication_types", "task_version": self.definition["task_version"],
            "definition_sha256": file_hash(self.base / "task-definition-v1.json"),
            "created_at": "2026-10-23T02:00:00+08:00",
            "evidence_offsets": "zero_based_unicode_codepoints_end_exclusive", "records": references}
        self.write(self.base / "inputs.json", {"records": self.rows})
        self.write(self.base / "astra-reference-v1.json", self.reference)
        self.write(self.original / "inputs.json", {"records": self.rows[:30]})
        self.write(self.original / "astra-reference-v1.json", {**self.reference, "records": references[:30]})
        config = {"definition": self.definition, "model_id": "Qwen/Qwen3-1.7B", "dtype": "float16", "revision": "frozen"}
        self.write(self.run / "manifest.json", {"config": config, "config_sha256": digest(config), "input_sha256": digest(self.rows)})
        self.write(self.run / "inputs.json", {"records": self.rows})
        self.write(self.run / "summary.json", {"state": "completed", "processed": 100, "training": False,
            "parameter_count": 1700000000, "peak_allocated_mib": 4000, "peak_reserved_mib": 4200,
            "nvml_process_peak_mib": 4400, "load_seconds": 10})
        for index, row in enumerate(self.rows):
            category = references[index]["category"] if index < 30 else (
                "non_neoplastic" if references[index]["category"] == "solid" else "solid")
            status = "classified"
            if index == 31:
                category, status = None, "invalid_output"
            self.write(self.run / f"result-{row['pmid']}.json", {
                **{key: row[key] for key in ("pmid", "version_id", "source_sha256", "input_sha256")},
                "config_sha256": digest(config), "category": category, "status": status,
                "raw_output": json.dumps({"category": category}), "seconds": 0.5})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def evaluate(self, **kwargs):
        return evaluate_runs(self.base, [self.run], sample_count=100, original_base=self.original, **kwargs)

    def test_expanded_scores_keep_new_samples_separate_and_failures_in_denominator(self):
        result = self.evaluate()
        self.assertEqual((result["sample_count"], result["denominator"], result["unscored"]), (100, 99, 1))
        self.assertEqual(result["methods"][0]["matched"], 30)
        original, added = result["groups"]["original"], result["groups"]["added"]
        self.assertEqual((original["sample_count"], added["sample_count"]), (30, 70))
        self.assertEqual((original["methods"][0]["matched"], original["denominator"]), (30, 30))
        self.assertEqual((added["methods"][0]["matched"], added["denominator"]), (0, 69))
        self.assertEqual(added["methods"][0]["failed"], 1)
        self.assertEqual(result["cases"][30]["group"], "added")

    def test_changed_original_reference_is_rejected(self):
        self.reference["records"][0]["category"] = "hematologic"
        self.write(self.base / "astra-reference-v1.json", self.reference)
        with self.assertRaisesMessage(ValueError, "Original cohort reference changed"):
            self.evaluate()

    def test_changed_original_source_is_rejected(self):
        self.rows[0]["version_id"] = "changed-version"
        self.reference["records"][0]["version_id"] = "changed-version"
        self.write(self.base / "inputs.json", {"records": self.rows})
        self.write(self.base / "astra-reference-v1.json", self.reference)
        with self.assertRaisesMessage(ValueError, "Original cohort input changed"):
            self.evaluate()

    def test_explicit_sample_count_does_not_silently_accept_a_partial_set(self):
        with self.assertRaisesMessage(ValueError, "Expected 30 frozen papers, got 100"):
            evaluate_runs(self.base, [self.run], sample_count=30)

    def test_public_record_uses_actual_counts_groups_and_supplied_date(self):
        evaluation = self.evaluate()
        args = Namespace(base=self.base, original_base=self.original, runs=[self.run],
            notes=None, id="expanded-test", title="Expanded test", date="2026-10-23")
        with patch("scripts.evaluate_model_comparison.ROOT", self.root):
            record = public_record(evaluation, args)
        prepare_record(record)
        self.assertEqual(record["date"], "2026-10-23")
        self.assertEqual(record["sample_count"], 100)
        self.assertIn("99篇确定参考", record["reference"]["note"])
        self.assertIn("统一100篇", record["dataset"]["label"])
        self.assertEqual(record["comparison"]["groups"]["added"]["denominator"], 69)
        new_metric = next(metric for metric in record["methods"][0]["metrics"] if metric["label"] == "新增70篇与参考一致")
        self.assertEqual((new_metric["numerator"], new_metric["denominator"]), (0, 69))
