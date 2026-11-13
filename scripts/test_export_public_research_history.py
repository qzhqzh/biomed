import copy
import unittest

from apps.curation.research_history import prepare_record
from scripts.export_public_research_history import metadata_summary


class PublicSummaryTests(unittest.TestCase):
    def test_preserves_measurements_without_exporting_source_text(self):
        original = {
            "schema_version": "research-history-v1", "id": "test-run", "title": "Test",
            "date": "2026-11-14", "stage": "classification", "status": "evaluated",
            "sample_count": 1, "summary": "Synthetic summary", "task": {"question": "Q"},
            "dataset": {"label": "Fixture", "raw_output": "private source"},
            "reference": {"label": "Reference"},
            "identity": {"task_version": "v1", "sample_versions": ["s"],
                         "method_versions": ["m"], "reference_versions": ["r"],
                         "completeness": "frozen"},
            "methods": [{"id": "x", "label": "X", "version": "v1", "metrics": [
                {"label": "score", "numerator": 1, "denominator": 1, "quote": "private source"}
            ], "conclusion": "Useful"}],
            "findings": ["private source"], "next_steps": [], "provenance": [],
            "cases": [{"pmid": "1", "model_status": "ok", "quote": "private source"}],
            "knowledge_candidates": [{"quote": "private source"}],
        }
        snapshot = copy.deepcopy(original)
        public = prepare_record(metadata_summary(original, "abc"))
        self.assertEqual(original, snapshot)
        self.assertNotIn("private source", str(public))
        self.assertEqual(public["methods"][0]["metrics"][0]["numerator"], 1)
        self.assertEqual(public["cases"][0]["pmid"], "1")
        self.assertEqual(public["provenance"][0]["sha256"], "abc")


if __name__ == "__main__":
    unittest.main()
