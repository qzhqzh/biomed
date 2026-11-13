import copy
import json
import tempfile
import unittest
from pathlib import Path

from apps.curation.decision import digest
from apps.curation.three_class import CATEGORIES
from apps.curation.three_class_evaluation import (
    evaluate,
    method_summary,
    validate_reference,
)


class ThreeClassEvaluationTests(unittest.TestCase):
    def setUp(self):
        metadata = {
            "title": "Lung cancer",
            "abstract": "",
            "mesh": [],
            "publication_types": [],
        }
        self.row = {
            "pmid": "123",
            "version_id": "v1",
            "source_sha256": "a" * 64,
            "input_sha256": digest(metadata),
            "metadata": metadata,
        }
        self.definition = {"task_version": "literature-three-class-v1"}
        self.reference = {
            "schema_version": "three-class-reference-v1",
            "annotator_model": "gpt-6-astra",
            "status": "model_reference_not_human_gold",
            "blinded_to_candidates": True,
            "input_scope": "title_abstract_mesh_publication_types",
            "task_version": "literature-three-class-v1",
            "definition_sha256": "d" * 64,
            "created_at": "2026-10-22T12:00:00Z",
            "evidence_offsets": "zero_based_unicode_codepoints_end_exclusive",
            "records": [
                {
                    **{
                        key: self.row[key]
                        for key in (
                            "pmid",
                            "version_id",
                            "source_sha256",
                            "input_sha256",
                        )
                    },
                    "category": "solid",
                    "reference_state": "determinate",
                    "reason": "明确肺癌",
                    "evidence": [
                        {"field": "title", "text": "Lung cancer", "start": 0, "end": 11}
                    ],
                }
            ],
        }

    def validate(self, reference):
        return validate_reference(reference, [self.row], self.definition, "d" * 64)

    def test_valid_reference_and_invalid_metadata(self):
        self.assertEqual(self.validate(self.reference)["123"]["category"], "solid")
        for key, value in (
            ("annotator_model", "other"),
            ("blinded_to_candidates", False),
            ("blinded_to_candidates", 1),
            ("definition_sha256", "a" * 64),
        ):
            reference = {**self.reference, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "metadata"):
                self.validate(reference)

    def test_missing_duplicate_source_and_inexact_quote_are_rejected(self):
        for operation in ("missing", "duplicate", "source", "quote", "offset", "state"):
            reference = copy.deepcopy(self.reference)
            row = reference["records"][0]
            if operation == "missing":
                reference["records"] = []
            elif operation == "duplicate":
                reference["records"].append(copy.deepcopy(row))
            elif operation == "source":
                row["input_sha256"] = "b" * 64
            elif operation == "quote":
                row["evidence"][0]["text"] = "Breast cancer"
            elif operation == "offset":
                row["evidence"][0]["start"] = 1
            else:
                row["reference_state"] = "unresolved"
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.validate(reference)

    def test_failures_remain_in_denominator_but_unresolved_reference_does_not(self):
        cases = []
        for reference, prediction, outcome, status in (
            ("solid", "solid", "matched", "validated_candidate"),
            ("solid", None, "failed", "invalid_output"),
            ("hematologic", "solid", "disagreed", "validated_candidate"),
            ("non_neoplastic", None, "abstained", "abstained"),
            (None, "solid", "unscored", "validated_candidate"),
        ):
            cases.append(
                {
                    "reference_relevance": reference,
                    "model_scope": prediction,
                    "model_outcome": outcome,
                    "model_status": status,
                }
            )
        summary = method_summary(cases, "model")
        self.assertEqual((summary["matched"], summary["denominator"]), (1, 4))
        self.assertEqual((summary["completed"], summary["total"]), (3, 5))
        self.assertEqual(summary["by_class"]["solid"], {"matched": 1, "denominator": 2})
        self.assertEqual(summary["confusion"]["solid"]["failed"], 1)
        self.assertAlmostEqual(summary["macro_recall"], 1 / 6)

    def test_missing_model_output_cannot_be_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "run").mkdir()
            definition = {
                **self.definition,
                "categories": list(CATEGORIES),
                "prompt": "Classify.",
            }
            definition_path = folder / "task-definition-v1.json"
            definition_path.write_text(json.dumps(definition))
            rows = [{**self.row, "pmid": str(100 + i)} for i in range(30)]
            for path in (folder / "inputs.json", folder / "run/inputs.json"):
                path.write_text(json.dumps({"records": rows}))
            reference = copy.deepcopy(self.reference)
            from apps.curation.three_class_evaluation import file_hash

            reference["definition_sha256"] = file_hash(definition_path)
            reference["records"] = [
                {**self.reference["records"][0], "pmid": row["pmid"]} for row in rows
            ]
            (folder / "astra-reference-v1.json").write_text(json.dumps(reference))
            config = {
                "definition": definition,
                "prompt": definition["prompt"],
                "model_id": "Qwen/Qwen3-1.7B",
            }
            manifest = {
                "config": config,
                "config_sha256": digest(config),
                "records": 30,
                "input_sha256": digest(rows),
            }
            (folder / "run/manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Missing model results"):
                evaluate(folder, [])
