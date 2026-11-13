import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.curation import test_entity_normalization as fixtures
from apps.curation.decision import digest
from scripts.benchmark_knowledge_candidates import load_inputs
from scripts.normalize_knowledge_entities import (
    MODEL_ID,
    MODEL_REVISION,
    cache_key,
    decode_result,
    run_normalization,
)
from scripts.prepare_knowledge_target_mapping import freeze, read


class NormalizationRunnerTests(unittest.TestCase):
    def test_format_recovery_never_invents_a_value_or_accepts_a_truncated_string(self):
        decision = fixtures.match({"contexts": [{"quote": "synthetic original quote"}]})
        raw = json.dumps(decision)
        parsed, repair = decode_result({"status": "invalid_json", "raw_output": raw[:-1], "payload": None})
        self.assertEqual(parsed, decision)
        self.assertEqual(repair, "appended_final_object_brace")
        with self.assertRaises(ValueError):
            decode_result({"status": "invalid_json", "raw_output": raw[:-5], "payload": None})

    def test_cache_reuses_reordering_but_not_a_changed_identity_question(self):
        question = {"label": "synthetic alias", "contexts": [{"quote": "test"}],
                    "candidates": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(cache_key(question), cache_key({**question, "candidates": question["candidates"][::-1]}))
        self.assertNotEqual(cache_key(question), cache_key({**question, "candidates": [{"id": "a"}]}))
        self.assertNotEqual(cache_key(question), cache_key({**question, "contexts": [{"quote": "changed"}]}))

    def test_real_runner_input_contract_bounded_correction_and_cached_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle, registry = root / "bundle.json", root / "registry.json"
            freeze(bundle, fixtures.bundle("another full alias"))
            freeze(registry, fixtures.registry())
            standards, oncotree, chembl = (root / name for name in ("standards.json", "oncotree.json", "chembl.json"))
            freeze(standards, {"concepts": [], "releases": {}, "genes": [], "gene_version": {}})
            freeze(oncotree, {"nodes": [], "version": "synthetic"})
            freeze(chembl, {"molecules": [], "version": "synthetic"})
            output = root / "run"

            def model(command, **kwargs):
                job = Path(command[command.index("--output") + 1])
                dataset = load_inputs(Path(command[command.index("--inputs") + 1]))
                config = {"model_id": MODEL_ID, "revision": MODEL_REVISION,
                          "definition": read(output / "definition.json"), "synthetic_test_only": True}
                config_sha = digest(config)
                freeze(job / "inputs.json", dataset)
                freeze(job / "manifest.json", {"config": config, "config_sha256": config_sha,
                                                       "input_sha256": digest(dataset),
                                                       "definition_sha256": digest(read(output / "definition.json"))})
                for row in dataset["records"]:
                    item = json.loads(row["prompt"])
                    decision = fixtures.match(item, candidate="local:" + fixtures.LOCAL if "validation_feedback" in item else "invented-id")
                    freeze(job / f"result-{row['id']}.json", {
                        "id": row["id"], "input_sha256": row["input_sha256"], "config_sha256": config_sha,
                        "status": "classified", "raw_output": json.dumps(decision), "payload": decision,
                        "input_tokens": 1, "output_tokens": 1, "seconds": 0.1})
                return SimpleNamespace(returncode=0)

            with patch("scripts.normalize_knowledge_entities.subprocess.run", side_effect=model) as invoke:
                first = run_normalization(bundle, registry, output, standards=standards, oncotree=oncotree, chembl=chembl, model_cache=root / "cache")
                cached = run_normalization(bundle, registry, output, use_model=False,
                                           standards=standards, oncotree=oncotree, chembl=chembl, model_cache=root / "cache")
            self.assertEqual(invoke.call_count, 2)  # First attempt and one bounded correction; replay calls neither.
            self.assertEqual(first["report"]["counts"], {"resolved": 1})
            self.assertEqual(cached["report"]["counts"], {"resolved": 1})
            self.assertEqual(cached["report"]["model_inference_records"], 0)
            self.assertEqual(first["report"]["correction_tasks"], 1)
            self.assertEqual(read(cached["patch"])["source"]["model"]["model_id"], MODEL_ID)
            self.assertEqual(read(first["registry"]), read(cached["registry"]))


if __name__ == "__main__":
    unittest.main()
