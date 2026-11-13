"""Queue invariants: claims, restart, idempotency, budgets and honest outcomes."""

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.curation.knowledge_processing import (
    Queue,
    atomic_json,
    batch_report,
    identity,
    source_page,
)
from scripts.knowledge_worker import Worker, configuration, host_pressure, process_start


def paper(number, **extra):
    return {"version_id": f"v{number}", "sha256": str(number) * 64, "material_id": f"m{number}",
            "pmid": str(number), "title": f"Cancer therapy trial {number}", "created_at": "2026-01-01",
            "has_knowledge": False, "priority": 1, **extra}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root)
        self.addCleanup(self.queue.close)

    def batch(self, count=5, **kwargs):
        self.queue.discover([paper(i) for i in range(1, count + 1)], "method", now=1)
        return self.queue.next_batch("method", 5, kwargs.get("daily", 10), kwargs.get("seconds", 1000), now=2)

    def test_priority_gate_leaves_other_sources_queued_without_filling_batch(self):
        self.queue.discover([paper(1, priority=4), paper(2, priority=1)], "method", now=1)
        batch = self.queue.next_batch("method", 5, None, None, now=2, minimum_priority=3)
        self.assertEqual([r["pmid"] for r in self.queue.papers(batch["id"])], ["1"])
        self.assertEqual(self.queue.db.execute("SELECT state FROM papers WHERE pmid='2'").fetchone()[0], "queued")

    def test_durable_two_batches_and_cross_process_deduplication(self):
        self.batch(12)
        other = Queue(self.root)
        self.addCleanup(other.close)
        self.assertEqual(other.discover([paper(i) for i in range(1, 13)], "method", now=3), 0)
        first = other.next_batch("method", 5, 10, 1000, now=3)
        other.start(first["id"], now=4)
        other.complete(first["id"], {"articles": [{"pmid": p["pmid"], "queue_state": "completed"} for p in other.papers(first["id"])]}, now=5)
        second = other.next_batch("method", 5, 10, 1000, now=6)
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse({p["pmid"] for p in other.papers(first["id"])} & {p["pmid"] for p in other.papers(second["id"])})
        other.start(second["id"], now=7)
        self.assertIsNone(other.next_batch("method", 5, 10, 1000, now=8))
        self.assertEqual(other.usage(8)["articles"], 10)
        self.assertIsNotNone(other.next_batch("method", 5, 10, 1000, now=86402))

    def test_interruption_resumes_same_batch_without_consuming_article_budget(self):
        batch = self.batch()
        self.queue.start(batch["id"], now=3)
        self.queue.charge(batch["id"], 30, now=4)
        self.queue.failure(batch["id"], "restart", interrupted=True, now=5)
        again = self.queue.next_batch("method", 5, 5, 1000, now=6)
        self.assertEqual(again["id"], batch["id"])
        self.assertEqual(again["attempts"], 0)
        self.assertEqual(self.queue.usage(6), {"articles": 5, "seconds": 30})
        self.queue.start(batch["id"], now=7)
        self.assertEqual(self.queue.db.execute("SELECT error FROM batches").fetchone()[0], "")

    def test_bounded_retries_isolate_a_failed_batch(self):
        batch = self.batch(10)
        for i, moment in enumerate((3, 100, 1000)):
            self.queue.start(batch["id"], now=moment)
            self.queue.failure(batch["id"], "process failed", now=moment + 1)
            state = self.queue.db.execute("SELECT state,next_attempt FROM batches WHERE id=?", (batch["id"],)).fetchone()
            self.assertEqual(state["state"], "failed" if i == 2 else "retry")
        next_one = self.queue.next_batch("method", 5, 10, 1000, now=2000)
        self.assertNotEqual(batch["id"], next_one["id"])
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM papers WHERE state='failed'").fetchone()[0], 5)

    def test_budget_and_pause_never_claim_new_work(self):
        batch = self.batch()
        self.queue.set("paused", True)
        self.assertIsNone(self.queue.next_batch("method", 5, 10, 100, now=3))
        self.queue.set("paused", False)
        self.queue.charge(batch["id"], 100, now=3)
        self.assertIsNone(self.queue.next_batch("method", 5, 10, 100, now=4))
        self.assertIsNotNone(self.queue.next_batch("method", 5, 10, 100, now=86405))

    def test_unlimited_continues_past_old_caps_without_resetting_usage(self):
        self.queue.discover([paper(i) for i in range(15)], "method", now=1)
        claimed = set()
        for moment in (2, 10, 20):
            batch = self.queue.next_batch("method", 5, None, None, now=moment)
            papers = self.queue.papers(batch["id"])
            self.assertEqual(len(papers), 5)
            self.assertFalse(claimed & {p["pmid"] for p in papers})
            claimed.update(p["pmid"] for p in papers)
            self.queue.start(batch["id"], now=moment + 1)
            self.queue.charge(batch["id"], 21601, now=moment + 2)
            self.queue.complete(batch["id"], {"articles": [
                {"pmid": p["pmid"], "queue_state": "completed"} for p in papers]}, now=moment + 3)
        self.assertEqual(self.queue.usage(24), {"articles": 15, "seconds": 64803})
        self.assertIsNone(self.queue.next_batch("method", 5, None, None, now=25))

    def test_unlimited_keeps_optional_caps_pause_and_retry_backoff(self):
        batch = self.batch(6, daily=None, seconds=None)
        self.queue.start(batch["id"], now=3)
        self.queue.charge(batch["id"], 100, now=4)
        self.queue.failure(batch["id"], "temporary error", now=5)
        # One unlimited dimension must not disable the other configured cap.
        self.assertIsNone(self.queue.next_batch("method", 5, None, 100, now=6))
        self.assertIsNone(self.queue.next_batch("method", 5, 5, None, now=6))
        following = self.queue.next_batch("method", 5, None, None, now=7)
        self.assertNotEqual(following["id"], batch["id"])
        self.queue.start(following["id"], now=8)
        self.queue.complete(following["id"], {"articles": [
            {"pmid": p["pmid"], "queue_state": "completed"} for p in self.queue.papers(following["id"])]}, now=9)
        self.assertIsNone(self.queue.next_batch("method", 5, None, None, now=64))
        self.queue.set("paused", True)
        self.assertIsNone(self.queue.next_batch("method", 5, None, None, now=65))
        self.queue.set("paused", False)
        self.assertEqual(self.queue.next_batch("method", 5, None, None, now=66)["id"], batch["id"])
        self.assertEqual(self.queue.usage(66), {"articles": 6, "seconds": 100})

    def test_legacy_and_notices_do_not_enter_model_queue(self):
        self.queue.discover([paper(1, has_knowledge=True), paper(2, title="Correction: Cancer trial"), paper(3)], "method", now=1)
        batch = self.queue.next_batch("method", 5, 10, 100, now=2)
        self.assertEqual([p["pmid"] for p in self.queue.papers(batch["id"])], ["3"])
        self.assertEqual(dict(self.queue.db.execute("SELECT state,count(*) FROM papers GROUP BY state")),
                         {"legacy": 1, "source_notice": 1, "assigned": 1})

    def test_method_upgrade_preserves_budget_completed_history_and_source_uniqueness(self):
        self.queue.discover([paper(1), paper(2), paper(3)], "old", now=1)
        batch = self.queue.next_batch("old", 1, 10, 100, now=2)
        self.assertEqual(self.queue.upgrade_pending_method("new", now=3), 0)
        self.queue.start(batch["id"], now=4)
        self.queue.complete(batch["id"], {"articles": [{"pmid": "1", "queue_state": "completed"}]}, now=5)
        before = self.queue.usage(5)
        self.assertEqual(self.queue.upgrade_pending_method("new", now=6), 2)
        self.assertEqual(self.queue.usage(6), before)
        self.assertEqual(self.queue.discover([paper(1), paper(2), paper(3)], "new", now=7), 0)
        self.assertEqual(dict(self.queue.db.execute("SELECT state,count(*) FROM papers GROUP BY state")), {"completed": 1, "queued": 2})
        self.assertEqual(self.queue.db.execute("SELECT method FROM papers WHERE state='completed'").fetchone()[0], "old")
        self.assertEqual(self.queue.upgrade_pending_method("new", now=8), 0)

    def test_incomplete_finalization_is_rolled_back(self):
        batch = self.batch()
        self.queue.start(batch["id"])
        with self.assertRaisesRegex(ValueError, "Missing paper"):
            self.queue.complete(batch["id"], {"articles": []})
        self.assertEqual(self.queue.db.execute("SELECT state FROM batches").fetchone()[0], "running")

    def test_three_failed_batches_pause_the_service(self):
        for i in range(3):
            self.queue.discover([paper(i + 1)], "method", now=i + 1)
            batch = self.queue.next_batch("method", 1, 10, 1000, now=i + 2)
            for _ in range(3):
                self.queue.start(batch["id"])
                self.queue.failure(batch["id"], "systemic")
        self.assertTrue(self.queue.get("paused"))

    def test_source_discovery_only_reads_parsed_valid_papers(self):
        path = self.root / "source.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE curation_material(id TEXT PRIMARY KEY,title TEXT,identifiers TEXT,created_at TEXT,kind TEXT);
                CREATE TABLE curation_materialversion(id TEXT PRIMARY KEY,sha256 TEXT,material_id TEXT,media_type TEXT,integrity_status TEXT);
                CREATE TABLE curation_passage(version_id TEXT,parser_version TEXT,locator TEXT,text TEXT,ordinal INTEGER);
                CREATE TABLE curation_knowledgecandidate(version_id TEXT);""")
            for i, kind in ((1, "paper"), (2, "guideline"), (3, "paper")):
                db.execute("INSERT INTO curation_material VALUES (?,?,?,?,?)", (f"m{i}", "Cancer therapy", json.dumps({"pubmed": str(i)}), "2026", kind))
                db.execute("INSERT INTO curation_materialversion VALUES (?,?,?,?,?)", (f"v{i}", "a"*64, f"m{i}", "application/jats+xml", "quarantined" if i == 3 else "valid"))
                db.execute("INSERT INTO curation_passage VALUES (?,?,?,?,?)", (f"v{i}", "text-v1-pypdf-6.20.0-jats-1", "body/p:1", "Cancer therapy", 0))
        db.close()
        before = path.read_bytes()
        with patch("apps.curation.knowledge_source_priority.local_genes", return_value={"symbols": {}, "aliases": {}}):
            rows, cursor = source_page(path)
        self.assertEqual([r["pmid"] for r in rows], ["1"])
        self.assertEqual(cursor, "")
        self.assertEqual(path.read_bytes(), before)

    def test_priority_refresh_only_changes_unclaimed_rows(self):
        self.queue.discover([paper(1), paper(2)], 'method', now=1)
        self.queue.next_batch('method', 1, None, None, now=2)
        before = [dict(r) for r in self.queue.db.execute('SELECT * FROM papers ORDER BY pmid')]
        self.queue.discover([paper(1, priority=3), paper(2, priority=3)], 'different-method', now=3)
        after = [dict(r) for r in self.queue.db.execute('SELECT * FROM papers ORDER BY pmid')]
        self.assertEqual(before[0], after[0])
        self.assertEqual({**before[1], 'priority': 3}, after[1])
        self.queue.discover([paper(2, priority=0, priority_reason={'text_deferred': True})], 'method', now=4)
        self.assertEqual(self.queue.db.execute("SELECT priority FROM papers WHERE pmid='2'").fetchone()[0], 3)

    def test_excluded_results_are_not_counted_as_extracted_knowledge(self):
        atomic_json(self.root / "batch-result.json", {"state": "no_solid_articles", "articles": [
            {"pmid": "1", "status": "classified", "category": "hematologic"},
            {"pmid": "2", "status": "invalid_category_output", "category": None}]})
        report = batch_report(self.root)
        self.assertEqual(report["knowledge_records"], 0)
        self.assertEqual([a["queue_state"] for a in report["articles"]], ["excluded", "failed"])
        self.assertEqual(report["quality_status"], "unreviewed")

    def test_final_report_requires_actual_database_receipt(self):
        atomic_json(self.root / "final/summary.json", {"articles": []})
        atomic_json(self.root / "final/knowledge.json", {"records": []})
        atomic_json(self.root / "final/import-receipt.json", {"bundle_sha256": "wrong", "result": {"entity_registry": {}}})
        with self.assertRaisesRegex(ValueError, "receipt"):
            batch_report(self.root)

    def test_target_reassessment_is_idempotent_and_preserves_independent_review(self):
        batch = self.batch(1)
        self.queue.start(batch["id"])
        self.queue.complete(batch["id"], {"articles": [{"pmid": "1", "queue_state": "partial"}],
                                           "reviewed_records": 3, "evaluation": {"reviewer": "actual reference"}})
        folder = self.root / "batches" / batch["id"] / "final"
        atomic_json(folder / "knowledge.json", {"records": [{"group_id": "test", "fact": {}, "assessment": {}}]})
        original = (folder / "knowledge.json").read_bytes()
        atomic_json(folder / "import-receipt.json", {"bundle_sha256": hashlib.sha256(original).hexdigest(),
                                                     "result": {"entity_registry": {}}})
        first = self.queue.refresh_target_readiness(batch["id"])
        self.assertEqual(first, self.queue.refresh_target_readiness(batch["id"]))
        summary = json.loads(self.queue.db.execute("SELECT summary FROM batches WHERE id=?", (batch["id"],)).fetchone()[0])
        self.assertEqual(summary["reviewed_records"], 3)
        self.assertEqual(summary["evaluation"], {"reviewer": "actual reference"})
        self.assertEqual((folder / "knowledge.json").read_bytes(), original)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM events WHERE kind='target_reassessed'").fetchone()[0], 1)
        atomic_json(folder / "import-receipt.json", {"bundle_sha256": "wrong"})
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.queue.refresh_target_readiness(batch["id"])

    def test_stale_process_is_recovered_but_live_process_is_not_duplicated(self):
        import os

        batch = self.batch()
        self.queue.start(batch["id"])
        self.queue.process(batch["id"], os.getpid(), process_start(os.getpid()))
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        self.assertFalse(worker.recover())
        worker.queue.process(batch["id"], os.getpid(), "old-boot")
        self.assertTrue(worker.recover())
        self.assertEqual(worker.queue.db.execute("SELECT state FROM batches").fetchone()[0], "retry")

    def test_operating_budgets_are_validated(self):
        for caps in ({}, {"daily_articles": None, "daily_runtime_seconds": None},
                     {"daily_articles": 10, "daily_runtime_seconds": 21600}):
            atomic_json(self.root / "config.json", caps)
            cfg = configuration(self.root)
            self.assertEqual(cfg["daily_articles"], caps.get("daily_articles"))
            self.assertEqual(cfg["daily_runtime_seconds"], caps.get("daily_runtime_seconds"))
        for key, value in (("daily_articles", 0), ("daily_runtime_seconds", False),
                           ("daily_runtime_seconds", "unlimited"), ("batch_size", None)):
            with self.subTest(key=key, value=value):
                atomic_json(self.root / "config.json", {key: value})
                with self.assertRaisesRegex(ValueError, key):
                    configuration(self.root)

    def test_worker_unlimited_still_waits_for_free_resources(self):
        self.queue.discover([paper(1)], "method")
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        with patch("scripts.knowledge_worker.source_page", return_value=([], "")), \
             patch.object(worker.queue, "usage", return_value={"articles": 100, "seconds": 86400}), \
             patch("scripts.knowledge_worker.resources_ready", return_value=("waiting_gpu", "busy")) as resources, \
             patch.object(worker, "execute") as execute:
            worker.tick()
            execute.assert_not_called()
            self.assertEqual(json.loads((self.root / "status.json").read_text())["activity"], "waiting_gpu")
            resources.return_value = ("ready", "")
            worker.tick()
            execute.assert_called_once()

    def test_supervisor_unlimited_does_not_interrupt_long_running_work(self):
        from unittest.mock import MagicMock

        batch = self.batch(1)
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        for limit in (None, 21600):
            with self.subTest(limit=limit):
                worker.cfg["daily_runtime_seconds"] = limit
                process = MagicMock(pid=999999, returncode=None)
                returns = iter([None, 0])
                process.wait.side_effect = lambda _process=process, _returns=returns, **kwargs: setattr(_process, "returncode", next(_returns))
                process.poll.side_effect = lambda _process=process: _process.returncode
                with patch.object(worker.queue, "usage", return_value={"articles": 20, "seconds": 21601}), \
                     patch("scripts.knowledge_worker.time.monotonic", side_effect=[0, 5, 10]), \
                     patch("scripts.knowledge_worker.os.killpg", side_effect=lambda *args, _process=process: setattr(_process, "returncode", -15)) as terminate:
                    if limit is None:
                        worker.supervise(process, self.root / "batches" / batch["id"], "pipeline")
                        terminate.assert_not_called()
                    else:
                        with self.assertRaisesRegex(InterruptedError, "每日任务时间预算"):
                            worker.supervise(process, self.root / "batches" / batch["id"], "pipeline")
                        terminate.assert_called_once()

    def test_supervisor_throttles_status_but_not_accounting(self):
        from unittest.mock import MagicMock

        batch = self.batch(1)
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        worker.cfg["daily_runtime_seconds"] = None
        process = MagicMock(pid=999999, returncode=None)
        returns = iter([None, None, None, None, None, 0])
        process.wait.side_effect = lambda **kwargs: setattr(process, "returncode", next(returns))
        process.poll.side_effect = lambda: process.returncode
        with patch.object(worker, "publish") as publish, \
             patch.object(worker.queue, "charge") as charge, \
             patch("scripts.knowledge_worker.time.monotonic", side_effect=[0, 5, 10, 15, 20, 25, 30]):
            worker.supervise(process, self.root / "batches" / batch["id"], "pipeline")
        self.assertEqual(charge.call_count, 6)
        self.assertEqual(publish.call_count, 3)

    def test_busy_source_database_does_not_block_frozen_batch_resume(self):
        self.batch()
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        with patch("scripts.knowledge_worker.source_page", side_effect=sqlite3.OperationalError("locked")), \
             patch("scripts.knowledge_worker.resources_ready", return_value=("ready", "")), \
             patch.object(worker, "execute") as execute:
            worker.tick()
        execute.assert_called_once()
        self.assertEqual(worker.queue.get("source_discovery")["state"], "retry_later")

    def test_discovery_preserves_completed_page_when_later_page_fails(self):
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        error = sqlite3.OperationalError("unexpected source error")
        with patch("scripts.knowledge_worker.source_page", side_effect=[([paper(1)], "v1"), error]):
            worker.discover_source()
        self.assertEqual(worker.queue.get("source_cursor"), "v1")
        self.assertEqual(worker.queue.get("source_discovery")["state"], "retry_later")
        self.assertEqual(worker.queue.get("source_last_error")["phase"], "source_read")
        self.assertEqual(worker.queue.db.execute("SELECT count(*) FROM papers").fetchone()[0], 1)
        with patch("scripts.knowledge_worker.source_page", return_value=([paper(2)], "")) as page:
            worker.discover_source()
        self.assertEqual(page.call_args.args[1], "v1")
        self.assertEqual(worker.queue.db.execute("SELECT count(*) FROM papers").fetchone()[0], 2)
        self.assertEqual(worker.queue.get("source_discovery")["state"], "available")
        self.assertEqual(worker.queue.get("source_last_error")["phase"], "source_read")

    def test_real_source_lock_retries_then_recovers_without_changing_source(self):
        path = self.root / "locked-source.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE curation_material(id TEXT PRIMARY KEY,title TEXT,identifiers TEXT,created_at TEXT,kind TEXT);
                CREATE TABLE curation_materialversion(id TEXT PRIMARY KEY,sha256 TEXT,material_id TEXT,media_type TEXT,integrity_status TEXT);
                CREATE TABLE curation_passage(version_id TEXT,parser_version TEXT,locator TEXT,text TEXT,ordinal INTEGER);
                CREATE TABLE curation_knowledgecandidate(version_id TEXT);""")
            db.execute("INSERT INTO curation_material VALUES (?,?,?,?,?)", ("m1", "Cancer therapy", '{"pubmed":"1"}', "2026", "paper"))
            db.execute("INSERT INTO curation_materialversion VALUES (?,?,?,?,?)", ("v1", "a"*64, "m1", "application/jats+xml", "valid"))
            db.execute("INSERT INTO curation_passage VALUES (?,?,?,?,?)", ("v1", "text-v1-pypdf-6.20.0-jats-1", "body/p:1", "Cancer therapy", 0))
        before = path.read_bytes()
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        worker.cfg["database"] = str(path)
        with sqlite3.connect(path) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            with patch("scripts.knowledge_worker.time.sleep") as sleep:
                worker.discover_source()
            sleep.assert_called_once_with(.5)
            self.assertEqual(worker.queue.get("source_discovery")["state"], "retry_later")
            self.assertEqual(worker.queue.get("source_last_error")["sqlite_errorcode"], sqlite3.SQLITE_BUSY)
            self.assertEqual(worker.queue.get("source_last_error")["phase"], "source_retry")
            writer.rollback()
        with patch("apps.curation.knowledge_source_priority.local_genes", return_value={"symbols": {}, "aliases": {}}):
            worker.discover_source()
        self.assertEqual(worker.queue.get("source_discovery")["state"], "available")
        self.assertEqual(worker.queue.db.execute("SELECT pmid FROM papers").fetchone()[0], "1")
        self.assertEqual(path.read_bytes(), before)

    def test_busy_host_waits_without_touching_other_services(self):
        (self.root / "pressure").mkdir()
        (self.root / "meminfo").write_text("MemAvailable: 30000000 kB\n")
        (self.root / "pressure/io").write_text("full avg10=0 avg60=0 avg300=0 total=0\n")
        (self.root / "pressure/memory").write_text("some avg10=0 avg60=0 avg300=0 total=0\n")
        self.assertEqual(host_pressure(self.root), "")
        (self.root / "pressure/io").write_text("full avg10=40 avg60=25 avg300=10 total=1\n")
        self.assertIn("繁忙", host_pressure(self.root))
        (self.root / "meminfo").write_text("MemAvailable: 4000000 kB\n")
        self.assertIn("内存不足", host_pressure(self.root))

    def test_independent_evaluation_is_bound_to_sample_and_never_changes_knowledge(self):
        batch = self.batch(1)
        self.queue.start(batch["id"])
        self.queue.complete(batch["id"], {"articles": [{"pmid": "1", "queue_state": "completed"}], "knowledge_records": 1})
        folder = self.root / "batches" / batch["id"]
        sample = {"records": [{"group_id": "g1", "anchors": [{"quote": "original evidence"}]}]}
        atomic_json(folder / "evaluation-sample.json", sample)
        payload = {"source_run": batch["id"], "sample_sha256": identity(sample), "reviewer": "test reference",
                   "records": [{"group_id": "g1", "decision": "unsupported", "reason": "wrong relationship",
                                "evidence_quotes": ["original evidence"], "issue_codes": ["wrong_relation"]}],
                   "lessons": ["Check relationship direction in the next method version."]}
        self.queue.record_evaluation(batch["id"], payload)
        self.queue.record_evaluation(batch["id"], payload)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM events WHERE kind='evaluated'").fetchone()[0], 1)
        self.assertEqual(self.queue.db.execute("SELECT state FROM papers").fetchone()[0], "completed")
        summary = self.queue.status("waiting", config={})["batches"][0]["summary"]
        self.assertEqual(summary["evaluation"]["counts"], {"unsupported": 1})
        self.assertEqual(summary["knowledge_records"], 1)
        self.assertEqual(read_json := json.loads((folder / "evaluation-sample.json").read_text()), sample)
        self.assertEqual(identity(read_json), payload["sample_sha256"])
        with self.assertRaisesRegex(ValueError, "different sample"):
            self.queue.record_evaluation(batch["id"], {**payload, "sample_sha256": "wrong"})
        payload["records"][0]["evidence_quotes"] = ["invented quote"]
        with self.assertRaisesRegex(ValueError, "source anchors"):
            self.queue.record_evaluation(batch["id"], payload)

    def test_email_retry_survives_without_another_completed_batch(self):
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)

        def fail(cfg, notices, key, subject, body, now):
            notices.setdefault("notices", {})[key] = {"attempts": 1, "error": "SMTPException"}

        def accept(cfg, notices, key, subject, body, now):
            notices["notices"][key] = {"sent_at": now}

        with patch("scripts.watch_literature.notify", side_effect=fail):
            worker.notify("completion", "result", "body")
        self.assertIn("completion", worker.queue.get("pending_notifications"))
        with patch("scripts.watch_literature.notify", side_effect=accept) as send:
            worker.deliver_notifications()
            worker.notify("completion", "result", "body")
        send.assert_called_once()
        self.assertEqual(worker.queue.get("pending_notifications"), {})

    def test_stalled_child_is_terminated_and_consumes_failure_retry(self):
        from unittest.mock import MagicMock

        batch = self.batch(1)
        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        worker.cfg["stall_seconds"] = 1
        process = MagicMock(pid=999999, returncode=None)
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 5)
        process.poll.side_effect = lambda: process.returncode

        def terminate(pid, signum):
            self.assertEqual(pid, 999999)
            process.returncode = -15

        with patch("scripts.knowledge_worker.subprocess.Popen", return_value=process), \
             patch("scripts.knowledge_worker.os.killpg", side_effect=terminate), \
             patch("scripts.knowledge_worker.time.monotonic", side_effect=[0, 2]), \
             self.assertRaisesRegex(RuntimeError, "长期无进展"):
            worker.child(["fixture"], self.root / "batches" / batch["id"], "pipeline")

    def test_ledger_failure_does_not_leave_an_unsupervised_model(self):
        from unittest.mock import MagicMock

        with patch("scripts.knowledge_worker.pipeline_method", return_value={"id": "method"}):
            worker = Worker(self.root)
        self.addCleanup(worker.queue.close)
        process = MagicMock(pid=999999)
        process.poll.return_value = -15
        with patch("scripts.knowledge_worker.subprocess.Popen", return_value=process), \
             patch.object(worker, "supervise", side_effect=sqlite3.OperationalError("locked")), \
             patch("scripts.knowledge_worker.os.killpg") as terminate, \
             self.assertRaises(sqlite3.OperationalError):
            worker.child(["fixture"], self.root / "batch", "pipeline")
        terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
