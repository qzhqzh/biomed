import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.curation.research_history import read_record, register_record
from scripts import follow_classification_batch_study as follow


class ClassificationFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue.sqlite3"
        self.db = sqlite3.connect(self.queue)
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE batches (id TEXT PRIMARY KEY, method TEXT, state TEXT, attempts INTEGER, next_attempt REAL, error TEXT, updated REAL, summary TEXT)"
        )
        for batch in follow.BATCHES:
            self.db.execute(
                "INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)",
                (
                    batch,
                    "method-v1",
                    "retry",
                    2,
                    100.0,
                    "temporary source issue",
                    99.0,
                    "{}",
                ),
            )
        self.db.commit()
        (self.root / "study-status.json").write_text(
            json.dumps({"state": "waiting", "reason": "busy", "step": "merged"})
        )

    def test_only_three_primary_keys_are_read_without_modifying_queue(self):
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM batches WHERE id IN (?,?,?)",
            follow.BATCHES,
        ).fetchall()
        self.assertTrue(any("INDEX" in row[-1].upper() for row in plan))
        before = self.queue.read_bytes()
        rows = follow.fetch_batches(self.queue)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["state"] == "retry" for row in rows))
        self.assertEqual(self.queue.read_bytes(), before)

    def test_waiting_report_has_no_fabricated_comparison_and_no_repeat_write(self):
        first = follow.refresh(self.root, self.queue)
        self.assertEqual(first["study"]["state"], "waiting")
        path = self.root / "followup-status.json"
        before = path.read_bytes()
        follow.refresh(self.root, self.queue)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(json.loads(before)["history"]), 1)
        report = (self.root / "followup-report.md").read_text()
        self.assertIn("尚无完整", report)
        self.assertNotIn("| 进程总耗时", report)

    def test_missing_report_is_regenerated_without_new_history(self):
        follow.refresh(self.root, self.queue)
        report_path = self.root / "followup-report.md"
        report_path.unlink()
        follow.refresh(self.root, self.queue)
        self.assertTrue(report_path.exists())
        self.assertEqual(
            len(
                json.loads((self.root / "followup-status.json").read_text())["history"]
            ),
            1,
        )

    def test_completion_report_uses_verified_values_and_labels_incomplete_metrics(self):
        report = {
            "matches": 9,
            "count": 10,
            "split_reference_matches": 8,
            "merged_reference_matches": 9,
            "split": {
                "model_loads": 2,
                "load_seconds": 200,
                "inference_seconds": 10,
                "wall_seconds": 220,
                "gpu_peak_mib": 9000,
                "host_peak_rss_kib": 1000,
                "process_read_bytes_sampled": 2048,
                "comparable_full_pass": True,
            },
            "merged": {
                "model_loads": 1,
                "load_seconds": 80,
                "inference_seconds": 10,
                "wall_seconds": 100,
                "gpu_peak_mib": None,
                "host_peak_rss_kib": None,
                "process_read_bytes_sampled": None,
                "comparable_full_pass": False,
            },
            "limitations": ["缓存未平衡"],
            "cases": [
                {
                    "pmid": "123",
                    "reference": "solid",
                    "split": "solid",
                    "merged": "solid",
                }
            ],
        }
        (self.root / "study-status.json").write_text(json.dumps({"state": "completed"}))
        (self.root / "comparison.json").write_text(json.dumps(report))
        with patch.object(follow, "evaluate", return_value=report):
            current = follow.refresh(self.root, self.queue)
        text = (self.root / "followup-report.md").read_text()
        self.assertEqual(current["comparison"], report)
        self.assertIn("9/10", text)
        self.assertIn("未测量", text)
        self.assertIn("不能作为完整单次", text)
        with (
            patch.object(follow, "evaluate", return_value={"different": True}),
            self.assertRaisesRegex(ValueError, "does not match"),
        ):
            follow.snapshot(self.root, self.queue)

    def test_terminal_batches_publish_once_without_calling_model(self):
        summary = {
            "articles": [{"queue_state": "partial"}] * 5,
            "knowledge_records": 3,
            "structurally_eligible": 2,
            "offline_ready_count": 0,
        }
        self.db.execute(
            "UPDATE batches SET state='completed', updated=1794590000, summary=?",
            (json.dumps(summary),),
        )
        self.db.commit()
        directory = self.root / "research"
        with patch.object(
            follow,
            "register_record",
            side_effect=lambda r: register_record(r, directory=directory),
        ):
            first = follow.refresh(self.root, self.queue)
            second = follow.refresh(self.root, self.queue)
        self.assertEqual(first, second)
        files = list(directory.glob("knowledge-retry-followup-20261114-*.json"))
        self.assertEqual(len(files), 1)
        record = read_record(files[0].stem, directory)
        self.assertEqual(record["sample_count"], 15)
        self.assertIn("3 个完成", record["summary"])
        self.assertEqual(record["methods"][0]["metrics"][2]["value"], 9)

    def test_changed_terminal_state_appends_new_record_without_overwriting_history(
        self,
    ):
        summary = {"articles": [{"queue_state": "partial"}] * 5, "knowledge_records": 3}
        self.db.execute(
            "UPDATE batches SET state='completed', updated=1794590000, summary=?",
            (json.dumps(summary),),
        )
        self.db.commit()
        directory = self.root / "research"
        with patch.object(
            follow,
            "register_record",
            side_effect=lambda r: register_record(r, directory=directory),
        ):
            follow.refresh(self.root, self.queue)
            first = next(directory.glob("knowledge-retry-followup-*.json"))
            before = first.read_bytes()
            self.db.execute(
                "UPDATE batches SET state='failed', updated=1794590100 WHERE id=?",
                (follow.BATCHES[0],),
            )
            self.db.commit()
            follow.refresh(self.root, self.queue)
        self.assertEqual(
            len(list(directory.glob("knowledge-retry-followup-*.json"))), 2
        )
        self.assertEqual(first.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
