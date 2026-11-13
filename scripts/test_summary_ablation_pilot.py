import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core_knowledge_pilot import PROMPT
from scripts.summary_ablation_pilot import (
    COMMON_KEYS,
    common_schema,
    compare_common_schema,
    prepare,
    remove_statement_prompt,
)


class SummaryAblationTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "id": "K01",
            "article": {},
            "passages": [
                {
                    "id": "p1",
                    "alias": "P1",
                    "role": "anchor",
                    "text": "Drug A improved survival.",
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
        self.result = {
            "status": "classified",
            "payload": {
                "fact": {
                    "relation": "treatment_outcome",
                    "drug_names": ["Drug A"],
                    "entity_ids": ["e1"],
                    "population": None,
                    "treatment": "Drug A",
                    "comparator": None,
                    "conditions": [],
                    "results": [],
                    "scope": "Current human trial",
                    "evidence": ["P1"],
                },
                "reason": None,
            },
        }

    def test_prompt_changes_only_two_statement_locations(self):
        line = next(
            line
            for line in PROMPT.splitlines(keepends=True)
            if line.startswith("- statement:")
        )
        expected = PROMPT.replace(line, "", 1).replace(
            "relation, statement, drug_names,", "relation, drug_names,", 1
        )
        self.assertEqual(remove_statement_prompt(PROMPT), expected)
        self.assertNotIn("source_quote", expected)
        with self.assertRaises(ValueError):
            remove_statement_prompt(PROMPT.replace("- statement:", "- summary:"))

    def test_common_fields_need_no_statement_and_never_use_it_as_support(self):
        baseline = deepcopy(self.result)
        baseline["payload"]["fact"]["statement"] = (
            "An unsupported summary is not evidence."
        )
        raw = deepcopy((self.packet, baseline, self.result))
        comparison = compare_common_schema(self.packet, baseline, self.result)
        left = comparison["arms"]["with_summary"]["raw"]
        right = comparison["arms"]["without_summary"]["raw"]
        self.assertEqual(left["status"], "common_structurally_valid")
        self.assertEqual(left["common_fact"], right["common_fact"])
        self.assertEqual(set(right["common_fact"]), COMMON_KEYS)
        self.assertNotIn("statement", right["common_fact"])
        self.assertEqual((self.packet, baseline, self.result), raw)

    def test_contract_rejects_extra_statement_or_source_quote(self):
        for field in ["statement", "source_quote"]:
            output = deepcopy(self.result)
            output["payload"]["fact"][field] = "Drug A improved survival."
            self.assertEqual(
                common_schema(self.packet, output, with_summary=False)["status"],
                "invalid_structure",
            )

    def test_unknown_refs_names_and_invalid_spans_are_rejected(self):
        cases = [
            ("entity_ids", ["unknown"], "unknown_entity_id"),
            ("evidence", ["P99"], "unknown_passage"),
            ("drug_names", ["Drug B"], "drug_name_not_literal"),
        ]
        for field, value, code in cases:
            with self.subTest(field=field):
                output = deepcopy(self.result)
                output["payload"]["fact"][field] = value
                checked = common_schema(self.packet, output, with_summary=False)
                self.assertIn(code, {i["code"] for i in checked["issues"]})
        self.packet["entities"][0]["locations"][0]["end"] = 4
        self.assertIn(
            "unlocated_upstream_entity",
            {
                i["code"]
                for i in common_schema(self.packet, self.result, with_summary=False)[
                    "issues"
                ]
            },
        )

    def test_case_alignment_is_separate_and_identical_in_both_arms(self):
        self.result["payload"]["fact"]["drug_names"] = ["drug a"]
        baseline = deepcopy(self.result)
        baseline["payload"]["fact"]["statement"] = "Drug A improved survival."
        comparison = compare_common_schema(self.packet, baseline, self.result)
        arms = list(comparison["arms"].values())
        self.assertEqual(arms[0]["name_changes"], arms[1]["name_changes"])
        for arm in arms:
            self.assertEqual(arm["raw"]["status"], "invalid_structure")
            self.assertEqual(arm["diagnostic"]["status"], "common_structurally_valid")

    def test_summary_cannot_supply_indirect_entity_support_to_common_diagnostics(self):
        self.packet["entities"].append(
            {
                "id": "e2",
                "type": "biomarker",
                "text": "Unlocated marker",
                "locations": [],
            }
        )
        self.result["payload"]["fact"]["entity_ids"].append("e2")
        baseline = deepcopy(self.result)
        baseline["payload"]["fact"]["statement"] = "Unlocated marker was associated."
        arms = compare_common_schema(self.packet, baseline, self.result)["arms"]
        self.assertEqual(
            arms["with_summary"]["entity_changes"],
            arms["without_summary"]["entity_changes"],
        )
        self.assertEqual(
            arms["with_summary"]["diagnostic"]["status"], "common_structurally_valid"
        )

    def test_prepare_copies_exact_input_bytes_without_model_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base, root = Path(temporary) / "baseline", Path(temporary) / "new"
            (base / "extract").mkdir(parents=True)
            rows = [{"id": f"K{i:02d}"} for i in range(1, 16)]
            for filename in ["inputs.json", "packets.json"]:
                (base / filename).write_text(
                    json.dumps({"records": rows}, separators=(",", ":"))
                )
            (base / "definition.json").write_text(json.dumps({"system_prompt": PROMPT}))
            config = {
                "model_id": "Qwen/Qwen3.5-9B",
                "dtype": "float16",
                "attention": "eager",
                "batch_size": 1,
                "do_sample": False,
                "enable_thinking": False,
                "max_input_tokens": 4096,
                "max_new_tokens": 2048,
            }
            (base / "extract" / "manifest.json").write_text(
                json.dumps({"config": config})
            )
            for row in rows:
                (base / "extract" / f"result-{row['id']}.json").write_text("{}")
            reference = Path(temporary) / "reference.json"
            reference.write_text("{}")
            prepare(root, reference, base)
            for filename in ["inputs.json", "packets.json"]:
                self.assertEqual(
                    (root / filename).read_bytes(), (base / filename).read_bytes()
                )
            self.assertEqual(
                json.loads((root / "definition.json").read_text())["system_prompt"],
                remove_statement_prompt(PROMPT),
            )
            self.assertFalse((root / "extract").exists())
            self.assertEqual(
                json.loads((root / "plan.json").read_text())["sample_count"], 15
            )


if __name__ == "__main__":
    unittest.main()
