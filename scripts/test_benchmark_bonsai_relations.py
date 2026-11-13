import unittest
from unittest.mock import patch

from scripts.benchmark_bonsai_relations import predict


class BonsaiRunnerTests(unittest.TestCase):
    def run_prediction(self, content, **extra):
        calls = []

        def request(endpoint, route, data):
            calls.append((route, data))
            if route == "/apply-template":
                return {"prompt": "frozen rendered prompt"}
            if route == "/tokenize":
                return {"tokens": [1, 2, 3]}
            return {"content": content, "tokens_predicted": 10, "stop_type": "eos", **extra}

        with patch("scripts.benchmark_bonsai_relations.request", side_effect=request):
            result = predict("http://localhost", {"system_prompt": "frozen"}, {"prompt": "original"})
        return result, calls

    def test_non_thinking_and_no_cache_or_schema_repair(self):
        result, calls = self.run_prediction('{"fact":null,"reason":"absent"}')
        self.assertEqual(result["status"], "classified")
        self.assertEqual(calls[0][1]["reasoning_effort"], "none")
        self.assertFalse(calls[0][1]["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(calls[-1][1]["cache_prompt"])
        self.assertEqual(calls[-1][1]["temperature"], 0)
        self.assertEqual(calls[-1][1]["n_predict"], 2048)
        self.assertNotIn("json_schema", calls[-1][1])

    def test_wrapped_output_stays_invalid(self):
        result, _ = self.run_prediction('```json\n{"fact":null}\n```')
        self.assertEqual(result["status"], "invalid_json")
        self.assertIsNone(result["payload"])

    def test_output_cap_cannot_be_rescued_by_valid_json(self):
        result, _ = self.run_prediction('{"fact":null}', stop_type="limit")
        self.assertEqual(result["status"], "output_truncated")
        self.assertIsNone(result["payload"])

    def test_server_input_truncation_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            self.run_prediction('{}', truncated=True)


if __name__ == "__main__":
    unittest.main()
