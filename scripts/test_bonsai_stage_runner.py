import unittest
from unittest.mock import patch

from scripts.benchmark_bonsai_stage import predict


class StageBudgetTests(unittest.TestCase):
    def test_stage_budget_and_exact_messages(self):
        calls = []

        def reply(endpoint, route, data):
            calls.append((route, data))
            if route == "/apply-template":
                return {"prompt": "rendered"}
            if route == "/tokenize":
                return {"tokens": [1, 2]}
            return {"content": '{"relevant":true}', "tokens_predicted": 5, "stop_type": "eos"}

        with patch("scripts.benchmark_bonsai_stage.request", side_effect=reply):
            result = predict("http://localhost", {"system_prompt": "frozen system"},
                             {"prompt": "frozen user"}, 6144, 64)
        self.assertEqual(calls[0][1]["messages"], [
            {"role": "system", "content": "frozen system"},
            {"role": "user", "content": "frozen user"},
        ])
        self.assertFalse(calls[0][1]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(calls[-1][1]["n_predict"], 64)
        self.assertFalse(calls[-1][1]["cache_prompt"])
        self.assertEqual(result["payload"], {"relevant": True})

    def test_over_budget_is_recorded_without_truncation_or_inference(self):
        with patch("scripts.benchmark_bonsai_stage.request", side_effect=[
            {"prompt": "original"}, {"tokens": [1, 2, 3]},
        ]) as mocked:
            result = predict("http://localhost", {"system_prompt": "s"}, {"prompt": "u"}, 2, 64)
        self.assertEqual(result["status"], "input_too_long")
        self.assertEqual(mocked.call_count, 2)
        self.assertIsNone(result["payload"])


if __name__ == "__main__":
    unittest.main()
