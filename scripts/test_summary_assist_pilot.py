import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from apps.curation.decision import digest
from scripts.evaluate_knowledge_candidates import read
from scripts.light_entity_pilot import runner_row
from scripts.summary_assist_pilot import (
    EXPECTED,
    PREFIX,
    prepare,
    prepare_assisted,
    token_preflight,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class SummaryAssistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "study"
        self.entity = self.base / "entity"
        self.relation = self.base / "relation"
        self.reference = self.base / "reference.json"
        write(self.reference, {"private_reference": "NEVER_LEAK_REFERENCE"})
        self.ids = [f"K{i:02}" for i in range(1, 16)]
        self.sources = [{
            "id": rid, "article": {"title": "An original title", "pmid": "123"},
            "passages": [
                {"id": "p1", "role": "anchor", "text": "Drug A improved survival in mice."},
                {"id": "p2", "role": "support", "text": "The study used a breast cancer model."},
            ],
            "prompt": "NEVER_LEAK_OLD_SOURCE_PROMPT",
        } for rid in self.ids]
        self.config = {**EXPECTED, "revision": "frozen-revision", "gpu_uuid": "GPU-test",
                       "runtime": {"transformers": "test"}}
        for stage, base in (("entity", self.entity), ("relation", self.relation)):
            inputs = {"records": [runner_row(rid, f"Original {stage}\n{rid}\nKeep trailing spaces.  \n") for rid in self.ids]}
            write(base / "inputs.json", inputs)
            write(base / "definition.json", {"version": stage, "system_prompt": "Unchanged system " + stage})
            write(base / "extract/manifest.json", {"config": self.config})
            for rid in self.ids:
                write(base / "extract" / f"result-{rid}.json", {"old_result": "NEVER_LEAK_OLD_RESULT"})
        write(self.entity / "source-inputs.json", {"records": self.sources})
        write(self.entity / "source-context.json", {"records": self.sources})
        write(self.entity / "guard-metadata.json", {"guard": "original"})
        packets = deepcopy(self.sources)
        for packet in packets:
            packet["entities"] = [{"text": "NEVER_LEAK_INVENTORY"}]
        write(self.relation / "packets.json", {"records": packets})

    def prepare(self):
        return prepare(self.root, self.reference, self.entity, self.relation)

    def summary_results(self):
        dataset = read(self.root / "summary-inputs.json")
        definition = read(self.root / "summary-definition.json")
        config = {**self.config, "definition": definition, "max_new_tokens": 512}
        manifest = {"config": config, "config_sha256": digest(config),
                    "input_sha256": digest(dataset), "definition_sha256": digest(definition)}
        write(self.root / "summary/manifest.json", manifest)
        for row in dataset["records"]:
            payload = {"summary": "Drug A improved survival in a breast cancer mouse model."}
            write(self.root / "summary" / f"result-{row['id']}.json", {
                "id": row["id"], "status": "classified", "payload": payload,
                "raw_output": json.dumps(payload), "input_sha256": row["input_sha256"],
                "config_sha256": manifest["config_sha256"],
            })

    def test_freezes_sources_and_does_not_leak_reference_inventory_or_old_outputs(self):
        self.assertEqual(self.prepare()["new_scored_calls"], 45)
        actual = (self.root / "summary-inputs.json").read_text()
        self.assertNotIn("NEVER_LEAK", actual)
        self.assertIn("An original title", actual)
        self.assertIn("Drug A improved survival in mice", actual)
        prompt = read(self.root / "summary-inputs.json")["records"][0]["prompt"]
        self.assertLess(prompt.index("SUPPORT"), prompt.index("ANCHOR"))
        for stage, base in (("entity", self.entity), ("relation", self.relation)):
            self.assertEqual((base / "definition.json").read_bytes(),
                             (self.root / f"{stage}-definition.json").read_bytes())
            self.assertEqual((base / "inputs.json").read_bytes(),
                             (self.root / "baseline" / stage / "inputs.json").read_bytes())
        self.assertEqual(self.reference.read_bytes(), (self.root / "reference.json").read_bytes())
        self.prepare()  # Repeated identical preparation is safe.
        write(self.entity / "inputs.json", {"records": [runner_row(rid, "changed") for rid in self.ids]})
        with self.assertRaisesRegex(ValueError, "Immutable artifact differs"):
            self.prepare()

    def test_exact_original_prompt_suffix_shared_summary_and_source_only_assembly(self):
        self.prepare()
        self.summary_results()
        self.assertEqual(prepare_assisted(self.root)["downstream_records"], 30)
        summary = read(self.root / "summary/result-K01.json")["payload"]["summary"]
        for stage, base in (("entity", self.entity), ("relation", self.relation)):
            before = read(base / "inputs.json")["records"]
            after = read(self.root / f"{stage}-inputs.json")["records"]
            for old, new in zip(before, after, strict=True):
                self.assertEqual(new["prompt"], PREFIX.format(summary=summary) + old["prompt"])
                self.assertEqual(new["input_sha256"], digest({"id": old["id"], "prompt": new["prompt"]}))
        for name in ("source-inputs.json", "source-context.json", "guard-metadata.json"):
            self.assertEqual((self.entity / name).read_bytes(),
                             (self.root / "entities" / name).read_bytes())
        provenance = read(self.root / "assisted-inputs-manifest.json")
        self.assertEqual(provenance["denominator"], 15)
        self.assertTrue(all(r["original_prompt_unchanged"] for r in provenance["records"]))
        prepare_assisted(self.root)

    def test_failed_or_null_summary_never_drops_denominator_or_creates_partial_arms(self):
        self.prepare()
        self.summary_results()
        for change in ({"status": "output_truncated"}, {"payload": None}, {"payload": {"summary": ""}}):
            with self.subTest(change=change):
                path = self.root / "summary/result-K09.json"
                result = read(path)
                write(path, {**result, **change})
                with self.assertRaisesRegex(ValueError, '"denominator": 15.*K09'):
                    prepare_assisted(self.root)
                self.assertFalse((self.root / "entity-inputs.json").exists())
                self.assertFalse((self.root / "relation-inputs.json").exists())
                self.summary_results()

    def test_tampered_payload_manifest_and_frozen_sources_are_rejected(self):
        self.prepare()
        self.summary_results()
        path = self.root / "summary/result-K03.json"
        result = read(path)
        result["payload"]["summary"] = "A manually rewritten summary"
        write(path, result)
        with self.assertRaisesRegex(ValueError, "K03"):
            prepare_assisted(self.root)
        self.summary_results()
        manifest_path = self.root / "summary/manifest.json"
        manifest = read(manifest_path)
        manifest["config"]["max_new_tokens"] = 2048
        write(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "manifest"):
            prepare_assisted(self.root)
        self.summary_results()
        (self.root / "baseline/entity/source-inputs.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Frozen source changed"):
            prepare_assisted(self.root)

    def test_real_tokenization_contract_never_truncates_or_removes_overlong_rows(self):
        self.prepare()
        calls = []

        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                calls.append((messages, kwargs))
                return messages[1]["content"]

            def __call__(self, prompt, **kwargs):
                self.assert_no_truncation = kwargs == {"truncation": False}
                if not self.assert_no_truncation:
                    raise AssertionError(kwargs)
                return {"input_ids": [0] * (4097 if "K09" in prompt else 4096)}

        result = token_preflight(read(self.entity / "inputs.json"),
                                 read(self.entity / "definition.json"), Tokenizer())
        self.assertFalse(result["ready"])
        self.assertEqual(result["denominator"], 15)
        self.assertEqual(len(result["records"]), 15)
        self.assertEqual(result["records"][8]["status"], "input_too_long")
        self.assertEqual(result["records"][0]["status"], "ready")
        self.assertEqual(calls[0][1], {"tokenize": False, "add_generation_prompt": True,
                                      "enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
