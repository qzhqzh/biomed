import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_bonsai_screening import interpreted, paired
from scripts.evaluate_passage_screening import metrics
from scripts.prepare_bonsai_screening import document, freeze, mapped_inputs
from apps.curation.decision import digest
from apps.curation.model_comparison import score_predictions


class BonsaiScreeningTests(unittest.TestCase):
    @staticmethod
    def raw(output, status="classified"):
        return {"status": status, "raw_output": output, "seconds": 1.0,
                "input_tokens": 5, "output_tokens": 5}

    def test_prompt_preserves_original_serialization_and_omits_labels(self):
        row = {"pmid": "1", "metadata": {"title": "肿瘤", "abstract": "A", "mesh": [], "publication_types": []},
               "input_sha256": "original", "reference": "do not send"}
        result = mapped_inputs("classification", [row])["records"][0]
        self.assertEqual(result["prompt"], json.dumps(row["metadata"], ensure_ascii=False))
        self.assertEqual(result["input_sha256"], digest({k: v for k, v in result.items() if k != "input_sha256"}))
        self.assertNotIn("reference", result)
        passage = {"title": "T", "locator": "L", "text": "C", "previous_text": "P", "next_text": "N", "label": True}
        self.assertEqual(document("passages", passage), '{"title": "T", "locator": "L", "text": "C", "previous_text": "P", "next_text": "N"}')

    def test_null_is_pending_and_extra_fields_are_invalid(self):
        for stage, field in (("classification", "category"), ("passages", "relevant")):
            result = interpreted(self.raw(json.dumps({field: None})), stage)
            self.assertEqual(result["status"], "abstained")
            self.assertIsNone(result[field])
        result = interpreted(self.raw('{"relevant":true,"reason":"extra"}'), "passages")
        self.assertEqual(result["status"], "invalid_output")
        self.assertIsNone(result["relevant"])

    def test_truncated_json_is_not_rescued(self):
        result = interpreted(self.raw('{"relevant":false}', "output_truncated"), "passages")
        self.assertIsNone(result["relevant"])
        self.assertEqual(result["status"], "output_truncated")

    def test_pending_negative_does_not_become_true_negative(self):
        rows = [{"id": "positive"}, {"id": "negative"}]
        refs = {"positive": {"relevant": True}, "negative": {"relevant": False}}
        predictions = {key: {"relevant": None} for key in refs}
        result = metrics(rows, refs, predictions)
        self.assertEqual(result["fn"], 1)
        self.assertEqual(result["tn"], 0)
        self.assertEqual(result["pending_all"], 2)

    def test_reference_unresolved_excluded_but_model_pending_kept(self):
        rows = [{"pmid": "1"}, {"pmid": "2"}]
        refs = {"1": {"category": "solid"}, "2": {"category": None}}
        prediction = {key: {"category": None, "status": "abstained"} for key in refs}
        result = score_predictions(rows, refs, prediction)
        self.assertEqual(result["denominator"], 1)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["abstained"], 1)
        comparisons = paired(rows, refs, {"qwen35-4b": prediction, "bonsai27b": prediction}, "qwen35-4b", "classification")
        self.assertEqual(comparisons["both_incorrect"]["ids"], ["1"])
        self.assertEqual(comparisons["unscored"]["ids"], ["2"])

    def test_freeze_reuses_exact_data_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inputs.json"
            freeze(path, {"a": 1})
            freeze(path, {"a": 1})
            with self.assertRaisesRegex(ValueError, "Frozen artifact changed"):
                freeze(path, {"a": 2})


if __name__ == "__main__":
    unittest.main()
