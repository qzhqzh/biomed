import copy
import json
import tempfile
import unittest
from pathlib import Path

from apps.curation.decision import digest, validate_decision
from apps.curation.evidence_pilot import atomic_json
from apps.curation.pilot_evaluation import REFERENCE_FILE, evaluate


class PilotReferenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "first20"
        self.folder.mkdir()
        self.output = self.root / "evaluation"

    def fixture(self, cases, *, folder=None):
        """Cases specify reference, rule scope, model state and optional model label."""
        folder = folder or self.folder
        folder.mkdir(exist_ok=True)
        model_folder = folder / "model"
        model_folder.mkdir(exist_ok=True)
        config = {
            "version": "test-v1",
            "model_id": "fixture-model",
            "profile": "multi_axis",
        }
        config_sha = digest(config)
        atomic_json(
            model_folder / "manifest.json",
            {"config": config, "config_sha256": config_sha},
        )
        documents, references = [], []
        for rank, case in enumerate(cases, 1):
            metadata = {
                "title": f"Fixture study {rank}",
                "abstract": "Synthetic test text.",
                "mesh": [],
                "publication_types": ["Journal Article"],
            }
            original = {
                "pmid": str(1000 + rank),
                "version_id": f"version-{rank}",
                "source_sha256": "a" * 64,
                "input_sha256": digest(metadata),
                "metadata": metadata,
            }
            document = {
                "rank": rank,
                "pmid": original["pmid"],
                "title": metadata["title"],
                "model_input": original,
                "input_scope": "abstract_only",
                "rule_baseline": {"scope": case["rule"], "labels": []},
            }
            raw = {
                "relevance": case["reference"],
                "reason": "Synthetic fixture decision.",
                "evidence": [{"field": "title", "quote": metadata["title"]}],
                "labels": [],
            }
            references.append(
                {
                    **{
                        key: value
                        for key, value in original.items()
                        if key != "metadata"
                    },
                    "raw_decision": raw,
                    "reference_state": case.get("state", "determinate"),
                    "acceptable_relevance": case.get("acceptable", [case["reference"]]),
                    "notes": "Fixture only.",
                }
            )
            if case["status"] != "not_run":
                model_raw = {**raw, "relevance": case.get("model", case["reference"])}
                raw_output = json.dumps(model_raw)
                decision = validate_decision(raw_output, metadata)
                # Preserve a plausible-looking decision in failed output to ensure
                # its presence cannot turn protocol failure into a prediction.
                atomic_json(
                    model_folder / f"result-{original['pmid']}.json",
                    {
                        **original,
                        "status": case["status"],
                        "decision": decision,
                        "raw_output": raw_output
                        if case["status"] == "validated_candidate"
                        else "broken output",
                        "config_sha256": config_sha,
                        "seconds": 0.5,
                        "error": ""
                        if case["status"] == "validated_candidate"
                        else "bad protocol",
                    },
                )
            documents.append(document)
        taxonomy = {"version": "fixture-rules-v1"}
        snapshot = {
            "documents": documents,
            "taxonomy": taxonomy,
            "taxonomy_sha256": digest(taxonomy),
        }
        atomic_json(
            folder / "snapshot.json", {"snapshot": snapshot, "sha256": digest(snapshot)}
        )
        atomic_json(
            folder / "model-inputs.json",
            {"records": [document["model_input"] for document in documents]},
        )
        reference = {
            "reference_version": "astra-reference-v1",
            "annotator_model": "gpt-6-astra",
            "status": "model_reference_not_human_gold",
            "blinded_to_candidates": True,
            "input_scope": "title_abstract_mesh_publication_types",
            "created_at": "2026-10-22T00:00:00+00:00",
            "records": references,
        }
        atomic_json(folder / REFERENCE_FILE, reference)
        return documents, reference

    def test_scope_denominator_includes_failures_and_uncertain_is_abstention(self):
        self.fixture(
            [
                {
                    "reference": "solid",
                    "rule": "solid_candidate",
                    "status": "invalid_output",
                },
                {
                    "reference": "hematologic",
                    "rule": "uncertain",
                    "status": "validated_candidate",
                    "model": "solid",
                },
                {"reference": "solid", "rule": "solid_candidate", "status": "not_run"},
                {
                    "reference": "non_neoplastic",
                    "rule": "uncertain",
                    "status": "validated_candidate",
                },
            ]
        )
        result = evaluate([self.folder], self.output)
        cohort = result["cohorts"][0]
        rules, model = cohort["methods"]
        self.assertEqual(cohort["scope_eligible"], 3)
        self.assertEqual(
            (rules["matched"], rules["denominator"], rules["abstained"]), (2, 3, 1)
        )
        self.assertEqual(
            (
                model["matched"],
                model["denominator"],
                model["failed"],
                model["disagreed"],
            ),
            (0, 3, 2, 1),
        )
        self.assertEqual(
            (
                cohort["model_relevance"]["matched"],
                cohort["model_relevance"]["denominator"],
            ),
            (1, 4),
        )
        self.assertIsNone(result["records"][0]["model_relevance"])
        self.assertEqual(result["records"][0]["model_raw_output_field"], "raw_output")
        self.assertEqual(result["records"][3]["rule_outcome"], "unscored")
        self.assertEqual(result["records"][3]["model_outcome"], "unscored")
        self.assertEqual(result["records"][3]["model_relevance_outcome"], "matched")
        self.assertEqual(model["model_id"], "fixture-model")
        self.assertTrue(result["cohort_sources"][0]["reference_sha256"])
        self.assertNotIn("accuracy", result)
        self.assertEqual(result["published_evidence"], 0)

    def test_model_uncertain_scope_abstains_even_when_format_is_valid(self):
        self.fixture(
            [
                {
                    "reference": "solid",
                    "rule": "uncertain",
                    "status": "validated_candidate",
                    "model": "oncology_unspecified",
                }
            ]
        )
        result = evaluate([self.folder], self.output)
        model = result["cohorts"][0]["methods"][1]
        self.assertEqual(
            (model["matched"], model["denominator"], model["abstained"]), (0, 1, 1)
        )
        self.assertEqual(result["records"][0]["model_relevance_outcome"], "disagreed")

    def test_ambiguous_reference_is_separate_from_both_scoring_denominators(self):
        self.fixture(
            [
                {
                    "reference": "solid",
                    "rule": "mixed_candidate",
                    "status": "validated_candidate",
                    "model": "hematologic",
                    "state": "ambiguous",
                    "acceptable": ["solid", "hematologic"],
                }
            ]
        )
        result = evaluate([self.folder], self.output)
        cohort, row = result["cohorts"][0], result["records"][0]
        self.assertEqual(cohort["reference_ambiguous"], 1)
        self.assertEqual(cohort["scope_eligible"], 0)
        self.assertTrue(all(method["denominator"] == 0 for method in cohort["methods"]))
        self.assertEqual(cohort["model_relevance"]["denominator"], 0)
        self.assertEqual(
            (row["rule_outcome"], row["model_outcome"], row["model_relevance_outcome"]),
            ("unscored", "unscored", "unscored"),
        )
        self.assertFalse(row["rule_acceptable_scope"])
        self.assertTrue(row["model_acceptable_scope"])
        self.assertTrue(row["model_acceptable_relevance"])

    def test_missing_duplicate_or_wrong_source_reference_is_refused(self):
        _, reference = self.fixture(
            [{"reference": "solid", "rule": "solid_candidate", "status": "not_run"}]
        )
        malformed = []
        missing = copy.deepcopy(reference)
        missing["records"] = []
        malformed.append(missing)
        duplicate = copy.deepcopy(reference)
        duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
        malformed.append(duplicate)
        wrong_hash = copy.deepcopy(reference)
        wrong_hash["records"][0]["input_sha256"] = "b" * 64
        malformed.append(wrong_hash)
        wrong_quote = copy.deepcopy(reference)
        wrong_quote["records"][0]["raw_decision"]["evidence"][0]["quote"] = (
            "Not in the source"
        )
        malformed.append(wrong_quote)
        for number, value in enumerate(malformed):
            with self.subTest(number=number):
                atomic_json(self.folder / REFERENCE_FILE, value)
                with self.assertRaises(ValueError):
                    evaluate([self.folder], self.output)
                self.assertFalse(self.output.exists())

    def test_missing_reference_produces_waiting_state_without_scores(self):
        self.fixture(
            [{"reference": "solid", "rule": "solid_candidate", "status": "not_run"}]
        )
        (self.folder / REFERENCE_FILE).unlink()
        result = evaluate([self.folder], self.output)
        self.assertEqual(result["state"], "waiting_reference")
        self.assertEqual(result["reference_status"], "waiting_reference")
        self.assertEqual(result["cohorts"][0]["methods"], [])
        self.assertIsNone(result["cohorts"][0]["scope_eligible"])
        self.assertEqual(result["records"], [])
        self.assertIn("waiting_reference", (self.output / "evaluation.md").read_text())

    def test_separate_cohorts_do_not_hide_first_sample_failures(self):
        self.fixture(
            [{"reference": "solid", "rule": "uncertain", "status": "invalid_output"}]
        )
        second = self.root / "solid10"
        self.fixture(
            [
                {
                    "reference": "solid",
                    "rule": "solid_candidate",
                    "status": "validated_candidate",
                }
            ],
            folder=second,
        )
        result = evaluate([self.folder, second], self.output)
        self.assertEqual(
            [cohort["id"] for cohort in result["cohorts"]], ["first20", "solid10"]
        )
        self.assertEqual(result["cohorts"][0]["methods"][1]["failed"], 1)
        self.assertEqual(result["cohorts"][1]["methods"][1]["matched"], 1)
        self.assertEqual(
            [row["cohort"] for row in result["records"]], ["first20", "solid10"]
        )


if __name__ == "__main__":
    unittest.main()
