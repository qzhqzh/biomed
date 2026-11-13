import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from apps.curation.decision import digest
from scripts.evaluate_bonsai_relations import (
    ARMS,
    IDS,
    build,
    checked_arm,
    mechanical_check,
    paired,
    validate_inputs,
)
from scripts.evaluate_knowledge_candidates import sha
from scripts.evaluate_summary_ablation import AXES, common_digest
from scripts.publish_bonsai_relations import card


class BonsaiRelationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.baseline = Path(self.temporary.name) / "baseline"
        self.root = Path(self.temporary.name) / "bonsai"
        self.packet = {
            "id": "K01",
            "article": {"pmid": "1", "title": "Test-only source"},
            "passages": [
                {
                    "id": "p1",
                    "alias": "P1",
                    "role": "anchor",
                    "text": "Drug A improved survival.",
                    "locator": "results",
                    "version_id": "v1",
                    "source_sha256": "source-sha",
                    "parser_version": "test",
                }
            ],
            "entities": [
                {
                    "id": "e1",
                    "type": "drug",
                    "text": "Drug A",
                    "locations": [{"passage_id": "p1", "start": 0, "end": 6}],
                }
            ],
        }
        self.fact = {
            "relation": "treatment_outcome",
            "drug_names": ["Drug A"],
            "entity_ids": ["e1"],
            "population": None,
            "treatment": "Drug A",
            "comparator": None,
            "conditions": [],
            "results": [],
            "scope": "Test fixture",
            "evidence": ["P1"],
        }
        self.item = {"id": "K01", "text": "Fixed test input"}
        self.item["input_sha256"] = digest(self.item)
        self.raw = {
            "id": "K01",
            "input_sha256": self.item["input_sha256"],
            "config_sha256": "test-config",
            "status": "classified",
            "payload": {"fact": self.fact, "reason": None},
            "seconds": 2.0,
            "input_tokens": 10,
            "output_tokens": 20,
        }
        self.raw["raw_output"] = json.dumps(self.raw["payload"])
        self.path = Path(self.temporary.name) / "result.json"
        self.write(self.path, self.raw)
        self.review = {
            "result_sha256": sha(self.path),
            "common_fields_sha256": common_digest(self.fact),
            "structured_common_core_usable": True,
            "axes": {axis: True for axis in AXES},
            "errors": [],
            "diagnostics": [],
            "deferred": [],
            "brief": "Test fixture, not an actual model judgment.",
        }

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def checked(self):
        return checked_arm(
            self.packet,
            self.raw,
            self.review,
            self.path,
            {"config_sha256": "test-config"},
            self.item,
        )

    def test_anchor_failure_cannot_be_counted_as_complete_task(self):
        self.review["axes"]["anchor_focus"] = False
        result = self.checked()
        self.assertTrue(result["diagnostic_core_ready"])
        self.assertFalse(result["complete_task_ready"])
        row = {
            "id": "K01",
            "arms": {
                "qwen9b": {**result, "complete_task_ready": True},
                "bonsai27b": result,
            },
        }
        self.assertEqual(
            paired([row], lambda arm: arm["complete_task_ready"])["qwen9b_only"]["ids"],
            ["K01"],
        )
        row["original_passages"] = self.packet["passages"]
        row["error_changes"] = {}
        public = card(self.packet, row)
        self.assertEqual(public["status"], "needs_revision")
        self.assertEqual(public["statement"], self.packet["passages"][0]["text"])

    def test_review_binds_raw_bytes_common_fact_and_exact_quote(self):
        with self.subTest("bytes"):
            self.path.write_text(self.path.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "identity differs"):
                self.checked()
            self.write(self.path, self.raw)
        with self.subTest("fact"):
            self.review["common_fields_sha256"] = "other"
            with self.assertRaisesRegex(ValueError, "raw fact"):
                self.checked()
            self.review["common_fields_sha256"] = common_digest(self.fact)
        with self.subTest("quote"):
            self.review["support"] = {
                "passage_id": "p1",
                "quote": "Drug B improved survival.",
            }
            with self.assertRaisesRegex(ValueError, "exact source English"):
                self.checked()

    def test_common_diagnostic_does_not_repair_wrong_source_name(self):
        raw = deepcopy(self.raw)
        raw["payload"]["fact"]["drug_names"] = ["Drug B"]
        self.assertEqual(
            mechanical_check(self.packet, raw)["diagnostic"]["status"],
            "invalid_structure",
        )
        raw["payload"]["fact"]["drug_names"] = ["drug a"]
        self.assertEqual(
            mechanical_check(self.packet, raw)["diagnostic"]["status"],
            "common_structurally_valid",
        )
        self.assertEqual(raw["payload"]["fact"]["drug_names"], ["drug a"])

    def fixture(self):
        definition = {
            "version": "unit-test",
            "system_prompt": "Frozen test instructions",
        }
        items, packets = [], []
        for rid in IDS:
            item = {"id": rid, "text": "Fixed test input"}
            item["input_sha256"] = digest(item)
            items.append(item)
            packets.append({**self.packet, "id": rid})
        reference = {
            "model": "gpt-6-astra",
            "policy_version": "unit-test-only",
            "records": [{"id": rid} for rid in IDS],
        }
        self.write(self.root / "reference.json", reference)
        reviews = {rid: {"id": rid, "arms": {}} for rid in IDS}
        for arm, run in zip(ARMS, (self.baseline, self.root), strict=True):
            dataset = {"records": items}
            for name, value in (
                ("inputs.json", dataset),
                ("packets.json", {"records": packets}),
                ("definition.json", definition),
            ):
                self.write(run / name, value)
            config = {
                "definition": definition,
                "model_id": arm,
                "max_new_tokens": 2048,
                "batch_size": 1,
                "do_sample": False,
                "enable_thinking": False,
            }
            manifest = {
                "records": 15,
                "config": config,
                "config_sha256": digest(config),
                "input_sha256": digest(dataset),
                "definition_sha256": digest(definition),
            }
            self.write(run / "extract/manifest.json", manifest)
            for item in items:
                raw = {
                    **self.raw,
                    "id": item["id"],
                    "input_sha256": item["input_sha256"],
                    "config_sha256": manifest["config_sha256"],
                }
                path = run / "extract" / f"result-{item['id']}.json"
                self.write(path, raw)
                reviews[item["id"]]["arms"][arm] = {
                    **self.review,
                    "result_sha256": sha(path),
                }
            self.write(
                run / "extract/summary.json",
                {
                    "state": "completed",
                    "processed": 15,
                    "expected": 15,
                    "config_sha256": manifest["config_sha256"],
                    "total_inference_seconds": 30.0,
                    "input_tokens": 150,
                    "output_tokens": 300,
                    "sessions": [{"wall_seconds": 30.0}],
                    "timing_comparable_full_pass": True,
                    "measurement_repetitions": 1,
                    "timing_scope": "test fixture",
                },
            )
        self.write(self.root / "plan.json", {"unit_test_only": True})
        self.write(
            self.root / "review.json",
            {
                "model": "gpt-6-astra",
                "status": "complete",
                "reference_sha256": sha(self.root / "reference.json"),
                "policy_version": reference["policy_version"],
                "records": list(reviews.values()),
            },
        )

    def test_complete_evaluation_is_reconstructible_and_never_reuses_old_score(self):
        self.fixture()
        result = build(self.root, self.baseline)
        self.assertEqual(result, build(self.root, self.baseline))
        self.assertEqual(result["scores"]["bonsai27b"]["complete_task_ready"], 15)
        self.assertEqual(result["timing"]["new_qwen9b_task_model_calls"], 0)
        self.assertEqual(
            result["paired"]["complete_task_ready"]["both_pass"]["count"], 15
        )

    def test_regenerated_input_is_rejected_even_with_valid_new_fingerprint(self):
        self.fixture()
        dataset = json.loads((self.root / "inputs.json").read_text())
        dataset["records"][6]["text"] = "New K07 input"
        item = dataset["records"][6]
        item["input_sha256"] = digest(
            {k: v for k, v in item.items() if k != "input_sha256"}
        )
        self.write(self.root / "inputs.json", dataset)
        with self.assertRaisesRegex(ValueError, "Frozen 014 inputs.json changed"):
            validate_inputs(self.root, self.baseline)


if __name__ == "__main__":
    unittest.main()
