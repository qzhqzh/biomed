import json
import tempfile
import unittest
from pathlib import Path

from apps.curation.decision import digest
from apps.curation.three_class import (
    CATEGORIES,
    load_completed,
    load_inputs,
    summarize,
    validate_output,
)


class ThreeClassTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
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

    def write_inputs(self, rows):
        path = self.folder / "inputs.json"
        path.write_text(json.dumps({"records": rows}))
        return path

    def test_only_three_categories_and_null_processing_state(self):
        for category in (*CATEGORIES, None):
            self.assertEqual(
                validate_output(json.dumps({"category": category})), category
            )
        for raw in (
            '{"category":"uncertain"}',
            '{"category":"benign"}',
            '{"category":"solid","reason":"x"}',
            "solid",
            "[]",
            '{"category":["solid"]}',
            '{"category":true}',
            '```json\n{"category":"solid"}\n```',
        ):
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)):
                validate_output(raw)

    def test_input_integrity_and_no_reference_labels(self):
        path = self.write_inputs([{**self.row, "reference": {"category": "solid"}}])
        self.assertNotIn("reference", load_inputs([path])[0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_inputs([path, path])
        self.row["metadata"]["title"] = "Changed source"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_inputs([self.write_inputs([self.row])])

    def result(self, category="solid", status="classified"):
        return {
            **{
                key: self.row[key]
                for key in ("pmid", "version_id", "source_sha256", "input_sha256")
            },
            "config_sha256": "b" * 64,
            "raw_output": json.dumps({"category": category}),
            "category": category,
            "status": status,
            "seconds": 1.5,
        }

    def test_checkpoint_provenance_and_raw_category_are_validated(self):
        path = self.folder / "result-123.json"
        result = self.result()
        path.write_text(json.dumps(result))
        self.assertEqual(
            load_completed(self.folder, [self.row], "b" * 64)["123"], result
        )
        for key, bad in (
            ("source_sha256", "c" * 64),
            ("config_sha256", "c" * 64),
            ("category", "hematologic"),
            ("status", "abstained"),
        ):
            path.write_text(json.dumps({**result, key: bad}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_completed(self.folder, [self.row], "b" * 64)

    def test_failures_and_abstentions_remain_in_summary_denominator(self):
        values = {
            "1": self.result(),
            "2": self.result(None, "abstained"),
            "3": self.result(None, "invalid_output"),
        }
        report = summarize([self.row] * 3, values)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["processed"], 3)
        self.assertEqual(report["categories"], {"solid": 1})
        self.assertEqual(
            report["statuses"], {"classified": 1, "abstained": 1, "invalid_output": 1}
        )
        self.assertEqual(report["inference_seconds"], 4.5)
