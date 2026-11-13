import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from apps.curation.decision import digest
from apps.curation.research_history import prepare_record, read_record, register_record
from scripts import follow_classification_batch_study as follow
from scripts import prepare_classification_batch_study as prepare
from scripts import run_classification_batch_study as study
from scripts.benchmark_knowledge_candidates import completed_results, load_inputs


class ClassificationBatchStudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base, self.model, self.output = (
            root / "source",
            root / "model",
            root / "output",
        )
        self.base.mkdir()
        self.model.mkdir()
        definition = {
            "task_version": "literature-three-class-v1",
            "prompt": "Classify as JSON.",
        }
        self.definition = self.base / "task-definition-v1.json"
        self.definition.write_text(json.dumps(definition))
        (self.model / "download-manifest.json").write_text(
            json.dumps({"repo": "fixture/model", "revision": "fixed"})
        )
        sources, refs = [], []
        number = 100
        for category, count in prepare.COUNTS.items():
            for _ in range(count):
                number += 1
                metadata = {
                    "title": f"Article {number}",
                    "abstract": "Abstract",
                    "mesh": [],
                    "publication_types": [],
                }
                source = {
                    "pmid": str(number),
                    "version_id": f"v{number}",
                    "source_sha256": "a" * 64,
                    "input_sha256": digest(metadata),
                    "metadata": metadata,
                }
                sources.append(source)
                refs.append(
                    {
                        k: source[k]
                        for k in ("pmid", "version_id", "source_sha256", "input_sha256")
                    }
                    | {"category": category, "reference_state": "determinate"}
                )
        (self.base / "inputs.json").write_text(json.dumps({"records": sources}))
        (self.base / "astra-reference-v1.json").write_text(
            json.dumps(
                {
                    "task_version": definition["task_version"],
                    "definition_sha256": prepare.file_hash(self.definition),
                    "records": refs,
                }
            )
        )
        prepare.prepare(
            self.output,
            base=self.base,
            model=self.model,
            production_definition=self.definition,
        )

    def validate(self, root):
        return study.validate_sample(root, expected_model=self.model)

    def write_step(self, name, inputs, *, change=None):
        folder = self.output / name
        folder.mkdir()
        data = load_inputs(self.output / inputs)
        (folder / "inputs.json").write_text(json.dumps(data))
        config = {"model": "identical-for-all-steps"}
        config_sha = digest(config)
        definition = json.loads((self.output / "definition.json").read_text())
        manifest = {
            "config": config,
            "config_sha256": config_sha,
            "input_sha256": digest(data),
            "definition_sha256": digest(definition),
        }
        (folder / "manifest.json").write_text(json.dumps(manifest))
        refs = {
            r["id"]: r["category"]
            for r in json.loads((self.output / "reference.json").read_text())["records"]
        }
        for row in data["records"]:
            value = "solid" if row["id"] == change else refs[row["id"]]
            result = {
                "id": row["id"],
                "input_sha256": row["input_sha256"],
                "config_sha256": config_sha,
                "status": "classified",
                "payload": {"category": value},
                "raw_output": json.dumps({"category": value}),
                "seconds": 0.2,
                "input_tokens": 10,
                "output_tokens": 2,
            }
            (folder / f"result-{row['id']}.json").write_text(json.dumps(result))
        summary = {
            "state": "completed",
            "config_sha256": config_sha,
            "processed": len(data["records"]),
            "sessions": [{"load_seconds": 2.0}],
            "total_inference_seconds": 1.0,
            "nvml_process_peak_mib": 9000,
            "nvml_errors": [],
            "timing_comparable_full_pass": True,
        }
        (folder / "summary.json").write_text(json.dumps(summary))
        metrics = {
            "attempts": [
                {
                    "state": "completed",
                    "elapsed_seconds": 4.0,
                    "ended_at": "2026-11-14T01:00:00+00:00",
                    "sampled_process_io_and_peak_rss": {
                        "read_bytes": 100,
                        "write_bytes": 10,
                        "peak_rss_kib": 200,
                    },
                }
            ]
        }
        (folder / "measurements.json").write_text(json.dumps(metrics))

    def test_frozen_inputs_are_balanced_identical_and_have_no_reference_labels(self):
        spec, _, data, refs = self.validate(self.output)
        self.assertEqual(len(data["records"]), 10)
        self.assertEqual(
            spec["counts"], {"solid": 4, "hematologic": 3, "non_neoplastic": 3}
        )
        self.assertTrue(all("category" not in row for row in data["records"]))
        self.assertEqual(len(refs), 10)
        self.assertEqual(
            prepare.prepare(
                self.output,
                base=self.base,
                model=self.model,
                production_definition=self.definition,
            )["sample_sha256"],
            spec["sample_sha256"],
        )

    def test_reference_or_input_tampering_is_rejected(self):
        path = self.output / "reference.json"
        value = json.loads(path.read_text())
        value["records"][0]["category"] = "hematologic"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "reference changed"):
            self.validate(self.output)

    def test_per_paper_checkpoint_prefix_is_resumable_but_gaps_are_rejected(self):
        self.write_step("merged", "inputs-all.json")
        folder = self.output / "merged"
        data = load_inputs(self.output / "inputs-all.json")
        config_sha = json.loads((folder / "manifest.json").read_text())["config_sha256"]
        paths = [folder / f"result-{row['id']}.json" for row in data["records"]]
        for path in paths[2:]:
            path.unlink()
        self.assertEqual(len(completed_results(folder, data["records"], config_sha)), 2)
        paths[0].unlink()
        with self.assertRaisesRegex(ValueError, "contiguous prefix"):
            completed_results(folder, data["records"], config_sha)

    def test_comparison_exposes_disagreement_and_requires_matching_model_config(self):
        non_solid = next(
            r["id"]
            for r in json.loads((self.output / "reference.json").read_text())["records"]
            if r["category"] != "solid"
        )
        self.write_step("merged", "inputs-all.json", change=non_solid)
        self.write_step("split-1", "inputs-split-1.json")
        self.write_step("split-2", "inputs-split-2.json")
        original = study.validate_sample
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
        self.assertEqual(report["matches"], 9)
        self.assertEqual(report["merged_reference_matches"], 9)
        self.assertEqual(report["split_reference_matches"], 10)
        self.assertEqual(report["split"]["model_loads"], 2)
        self.assertEqual(report["merged"]["model_loads"], 1)
        manifest_path = self.output / "split-2/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["config"]["model"] = "changed"
        manifest_path.write_text(json.dumps(manifest))
        with (
            patch.object(
                study,
                "validate_sample",
                side_effect=lambda root: original(root, expected_model=self.model),
            ),
            self.assertRaisesRegex(ValueError, "identity differs"),
        ):
            study.evaluate(self.output)

    def test_completed_runs_reuse_checkpoints_without_loading_models(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        original = study.validate_sample
        with (
            patch.object(
                study,
                "validate_sample",
                side_effect=lambda root: original(root, expected_model=self.model),
            ),
            patch.object(study, "run_step") as run,
            patch.object(study, "publish") as publish,
        ):
            self.assertEqual(study.study(self.output), 0)
        run.assert_not_called()
        self.assertEqual(publish.call_args.args[1]["matches"], 10)

    def test_interruption_stops_only_study_child_and_retains_existing_result(self):
        folder = self.output / "merged"
        folder.mkdir()
        checkpoint = folder / "result-existing.json"
        checkpoint.write_text("keep")
        process = MagicMock(pid=123456, returncode=None)
        process.poll.side_effect = lambda: process.returncode
        spec = json.loads((self.output / "sample.json").read_text())

        def stop_child(child):
            child.returncode = -15

        with (
            patch.object(study.subprocess, "Popen", return_value=process),
            patch.object(study, "read_process", return_value={"read_bytes": 10}),
            patch.object(
                study,
                "pressure",
                return_value={
                    "available_gib": 20,
                    "memory_some_60": 0,
                    "io_full_10": 0,
                },
            ),
            patch.object(study, "service_busy", return_value=True),
            patch.object(study, "halt_child", side_effect=stop_child) as halt,
        ):
            attempt = study.run_step(self.output, spec, "merged", "inputs-all.json")
        halt.assert_called_once_with(process)
        self.assertEqual(attempt["state"], "interrupted")
        self.assertEqual(checkpoint.read_text(), "keep")

    def test_busy_or_night_resource_gate_does_not_launch_model(self):
        night = datetime(2026, 11, 14, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.object(study, "service_busy") as services:
            self.assertIn("时段", study.start_gate(night))
        services.assert_not_called()
        day = datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with (
            patch.object(study, "service_busy", return_value=True),
            patch.object(study.subprocess, "run") as run,
        ):
            self.assertIn("运行中", study.start_gate(day))
        run.assert_not_called()

    def test_only_one_supervisor_can_update_the_same_study(self):
        with (
            study.study_lock(self.output),
            self.assertRaises(BlockingIOError),
            study.study_lock(self.output),
        ):
            pass
        with study.study_lock(self.output):
            pass

    def test_published_record_is_valid_and_marks_reference_as_model_based(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        original = study.validate_sample
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
        with patch.object(
            study, "register_record", side_effect=lambda record: prepare_record(record)
        ):
            record = study.publish(self.output, report)
        self.assertEqual(
            record["reference"]["status"], "model_reference_not_human_gold"
        )
        self.assertEqual(record["sample_count"], 10)
        self.assertEqual(
            sum(item.startswith("PMID ") for item in record["findings"]), 10
        )

    def test_missing_io_sample_is_not_reported_as_zero_or_clean_comparison(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        path = self.output / "split-1/measurements.json"
        metrics = json.loads(path.read_text())
        metrics["attempts"][0]["sampled_process_io_and_peak_rss"].pop("read_bytes")
        path.write_text(json.dumps(metrics))
        original = study.validate_sample
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
        self.assertIsNone(report["split"]["process_read_bytes_sampled"])
        self.assertFalse(report["split"]["comparable_full_pass"])
        with patch.object(
            study, "register_record", side_effect=lambda record: prepare_record(record)
        ):
            record = study.publish(self.output, report)
        split = next(method for method in record["methods"] if method["id"] == "split")
        self.assertNotIn(
            "采样磁盘读取", [metric["label"] for metric in split["metrics"]]
        )
        self.assertTrue(any("缺少进程 I/O" in item for item in record["findings"]))

    def test_missing_load_or_gpu_measurement_is_not_reported_as_zero(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        path = self.output / "merged/summary.json"
        summary = json.loads(path.read_text())
        summary["sessions"][0].pop("load_seconds")
        summary["nvml_process_peak_mib"] = 0
        path.write_text(json.dumps(summary))
        original = study.validate_sample
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
        self.assertIsNone(report["merged"]["model_loads"])
        self.assertIsNone(report["merged"]["load_seconds"])
        self.assertIsNone(report["merged"]["gpu_peak_mib"])
        self.assertFalse(report["merged"]["comparable_full_pass"])

    def test_completed_report_registration_is_immutable_and_idempotent(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        original = study.validate_sample
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
        directory = Path(self.temp.name) / "research"
        with patch.object(
            study,
            "register_record",
            side_effect=lambda record: register_record(record, directory=directory),
        ):
            first = study.publish(self.output, report)
            second = study.publish(self.output, report)
        self.assertEqual((first["status"], second["status"]), ("created", "reused"))
        record = read_record("classification-batching-20261114", directory)
        self.assertEqual(record["sample_count"], 10)

    def test_verified_completed_comparison_flows_to_followup_report(self):
        for name, inputs in study.STEPS:
            self.write_step(name, inputs)
        original = study.validate_sample
        queue = self.output / "followup-queue.sqlite3"
        with sqlite3.connect(queue) as connection:
            connection.execute(
                "CREATE TABLE batches (id TEXT PRIMARY KEY, method TEXT, state TEXT, attempts INTEGER, next_attempt REAL, error TEXT, updated REAL, summary TEXT)"
            )
            for batch in follow.BATCHES:
                connection.execute(
                    "INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)",
                    (batch, "method", "retry", 2, 0, "", 0, "{}"),
                )
        with patch.object(
            study,
            "validate_sample",
            side_effect=lambda root: original(root, expected_model=self.model),
        ):
            report = study.evaluate(self.output)
            (self.output / "comparison.json").write_text(json.dumps(report))
            (self.output / "study-status.json").write_text(
                json.dumps({"state": "completed"})
            )
            current = follow.refresh(self.output, queue)
        self.assertEqual(current["comparison"]["matches"], 10)
        text = (self.output / "followup-report.md").read_text()
        self.assertIn("10/10", text)
        self.assertIn("两次各5篇", text)
        self.assertIn("进程采样磁盘读取", text)


if __name__ == "__main__":
    unittest.main()
