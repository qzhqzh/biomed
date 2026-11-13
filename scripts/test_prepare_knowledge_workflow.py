import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.evaluate_knowledge_candidates import read
from scripts.light_entity_pilot import passage_prompt, runner_row
from scripts.prepare_knowledge_workflow import prepare, relation_prompt


class KnowledgePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packet = {
            "id": "K01", "article": {"pmid": "123", "title": "Original article", "rights": "retained"},
            "anchor_id": "p1", "passages": [{
                "id": "p1", "alias": "P1", "role": "anchor", "text": "Drug A improved survival.",
                "locator": "body/p:1", "version_id": "v1", "source_sha256": "a" * 64,
                "parser_version": "parser-v1",
            }], "entities": [{
                "id": "e1", "type": "drug", "text": "Drug A",
                "locations": [{"passage_id": "p1", "start": 0, "end": 6}],
            }],
        }

    def run_prepare(self, packets, stage="relations"):
        source = self.root / "packets.json"
        source.write_text(json.dumps({"records": packets}))
        return prepare(stage, source, self.root / "output")

    def test_unlocated_extra_is_deferred_without_blocking_or_losing_source(self):
        self.packet["entities"].append({"id": "extra", "type": "gene", "text": "Absent", "locations": []})
        result = self.run_prepare([self.packet])
        self.assertEqual(result["deferred_entities"], 1)
        output = self.root / "output"
        self.assertEqual({p.name for p in output.iterdir()}, {"inputs.json", "definition.json", "source-context.json", "plan.json"})
        context = read(output / "source-context.json")["records"][0]
        self.assertEqual(context["entities"], self.packet["entities"])
        self.assertEqual(context["article"], self.packet["article"])
        self.assertEqual(context["passages"], self.packet["passages"])
        self.assertEqual(context["deferred_entities"], [{"entity_id": "extra", "reason": "not_located"}])
        prompt = read(output / "inputs.json")["records"][0]["prompt"]
        self.assertNotIn("extra |", prompt)
        self.assertIn("e1 | drug | Drug A", prompt)
        definition = read(output / "definition.json")
        self.assertNotIn("- statement:", definition["system_prompt"])
        self.run_prepare([self.packet])

    def test_bad_input_is_rejected_before_writing(self):
        cases = []
        duplicate = deepcopy(self.packet)
        cases.append([duplicate, deepcopy(duplicate)])
        duplicate = deepcopy(self.packet)
        duplicate["entities"].append(deepcopy(duplicate["entities"][0]))
        cases.append([duplicate])
        invalid = deepcopy(self.packet)
        invalid["entities"][0]["locations"][0]["end"] = 5
        cases.append([invalid])
        empty = deepcopy(self.packet)
        empty["entities"][0]["locations"] = []
        cases.append([empty])
        no_anchor = deepcopy(self.packet)
        no_anchor["passages"][0]["role"] = "support"
        cases.append([no_anchor])
        glossary = deepcopy(self.packet)
        glossary["passages"][0]["ancestor_tags"] = ["def", "def-list"]
        cases.append([glossary])
        for packets in cases:
            with self.subTest(packets=packets), self.assertRaises(ValueError):
                self.run_prepare(packets)
            self.assertFalse((self.root / "output").exists())

    @unittest.skipUnless(Path("var/experiments/lean-entities-20261029/inputs.json").exists()
                         and Path("var/experiments/summary-ablation-20261029/inputs.json").exists(),
                         "Frozen local research artifacts are not part of the repository")
    def test_actual_010_014_input_bytes_and_definitions_match(self):
        entity = Path("var/experiments/lean-entities-20261029")
        relation = Path("var/experiments/summary-ablation-20261029")
        entity_source = entity / "source-inputs.json"
        relation_source = relation / "packets.json"
        for base, source, prompt in ((entity, entity_source, passage_prompt),
                                     (relation, relation_source, relation_prompt)):
            packets = read(source)["records"]
            expected = read(base / "inputs.json")["records"]
            self.assertEqual([runner_row(p["id"], prompt(p)) for p in packets], expected)
        for stage, source, base in (("entities", entity_source, entity), ("relations", relation_source, relation)):
            output = self.root / stage
            result = prepare(stage, source, output)
            self.assertEqual(result["records"], 15)
            self.assertEqual((output / "definition.json").read_bytes(), (base / "definition.json").read_bytes())
        self.assertEqual((self.root / "entities/inputs.json").read_bytes(), (entity / "inputs.json").read_bytes())
        changed = [old["id"] for old, new in zip(read(relation / "inputs.json")["records"],
                   read(self.root / "relations/inputs.json")["records"], strict=True) if old != new]
        self.assertEqual(changed, ["K07"])
        self.assertEqual(read(self.root / "relations/plan.json")["deferred_entities"],
                         [{"packet_id": "K07", "entity_id": "K07.e20", "reason": "not_located"}])


if __name__ == "__main__":
    unittest.main()
