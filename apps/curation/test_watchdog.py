import json
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.curation import collections
from apps.curation.collection_recovery import alert_pause
from apps.curation.management.commands.literature_guard import (
    limits_hash,
    recoverable_pause,
)
from scripts import watch_literature as watch


class WatchdogTests(SimpleTestCase):
    def test_only_confirmed_docker_startup_uses_wait_path(self):
        values = "ActiveState=activating\nInvocationID=invocation\nExecMainStartTimestampMonotonic=10000000\n"
        with (patch.object(watch, "command", return_value=values),
              patch.object(Path, "read_text", return_value="boot\n"),
              patch.object(watch.time, "monotonic", return_value=40)):
            self.assertEqual(watch.docker_startup({}),
                             {"identity": "boot:invocation", "elapsed_seconds": 30})
        with patch.object(watch, "command", return_value=values.replace("activating", "failed")):
            self.assertIsNone(watch.docker_startup({}))

    def test_docker_startup_does_not_consume_worker_recovery_attempts(self):
        state = {"probe_failures": 2, "actions": [{"kind": "restart", "at": 1}]}
        original = json.loads(json.dumps(state))
        with (patch.object(watch, "docker_startup", return_value={"identity": "boot:start", "elapsed_seconds": 1799}),
              patch.object(watch, "notify") as notify):
            self.assertTrue(watch.wait_for_docker_startup({}, state, 100))
        notify.assert_not_called()
        self.assertEqual(state["actions"], original["actions"])
        self.assertEqual(state["probe_failures"], original["probe_failures"])
        with (patch.object(watch, "docker_startup", return_value={"identity": "boot:start", "elapsed_seconds": 1800}),
              patch.object(watch, "notify") as notify):
            self.assertTrue(watch.wait_for_docker_startup({}, state, 101))
        self.assertEqual(notify.call_args.args[2], "docker-startup:boot:start")
        with patch.object(watch, "docker_startup", return_value=None):
            self.assertFalse(watch.wait_for_docker_startup({}, state, 102))
        self.assertEqual(state["last_progress"], 102)

    def test_startup_notice_already_accepted_does_not_read_mail_secrets(self):
        state = {"notices": {"docker-startup:boot:start": {"sent_at": 100}}}
        with patch.object(Path, "read_text") as read:
            watch.notify({}, state, "docker-startup:boot:start", "subject", "body", 101)
        read.assert_not_called()

    def test_startup_dry_run_does_not_write_state_or_send_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            state_path = Path(directory) / "state.json"
            config.write_text(json.dumps({"state_file": str(state_path)}))
            state_path.write_text('{"probe_failures": 2}')
            with (patch("sys.argv", ["watch", "--config", str(config), "--dry-run"]),
                  patch.object(watch, "inspect_storage", return_value=None),
                  patch.object(watch, "docker_startup", return_value={"identity": "boot:start", "elapsed_seconds": 1900}),
                  patch.object(watch, "notify") as notify,
                  patch.object(watch, "guard") as guard,
                  patch("builtins.print")):
                watch.main()
            self.assertEqual(state_path.read_text(), '{"probe_failures": 2}')
            notify.assert_not_called()
            guard.assert_not_called()

    def test_docker_startup_wait_preserves_existing_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            state_path = Path(directory) / "state.json"
            config.write_text(json.dumps({"state_file": str(state_path)}))
            state_path.write_text('{"halted": "operator review required", "probe_failures": 3}')
            with (patch("sys.argv", ["watch", "--config", str(config)]),
                  patch.object(watch, "docker_startup", return_value={"identity": "boot:start", "elapsed_seconds": 10}),
                  patch.object(watch, "guard") as guard,
                  patch.object(watch, "act") as act):
                watch.main()
            state = json.loads(state_path.read_text())
            self.assertEqual(state["halted"], "operator review required")
            self.assertEqual(state["probe_failures"], 3)
            guard.assert_not_called()
            act.assert_not_called()

    def test_guard_targets_only_named_worker_and_keeps_collection_and_limits(self):
        cfg = {"collection_id": "fixed-collection", "limits_hash": "same"}
        with patch.object(watch, "command", return_value="{}") as command:
            watch.guard(cfg)
        command.assert_called_once_with(
            cfg,
            ["docker", "exec", "biomed-worker", "python", "manage.py",
             "literature_guard", "fixed-collection", "--action", "status"],
        )
        with patch.object(watch, "command", return_value="{}") as command:
            watch.guard(cfg, "resume")
        self.assertEqual(command.call_args.args[1][-4:],
                         ["--action", "resume", "--limits-hash", "same"])

    def test_guard_uses_temporary_worker_only_when_singleton_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".curation-worker-recovery.pid").write_text("123")
            lock_path = root / ".curation-worker-heartbeat.lock"
            with lock_path.open("w") as lock, patch.object(watch, "recovery_process_matches", return_value=True) as match:
                self.assertFalse(watch.recovery_worker_active({"storage_paths": [directory]}))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(watch.recovery_worker_active({"storage_paths": [directory]}))
                match.assert_called_with(os.getpid(), 123)
            self.assertFalse(watch.recovery_worker_active({"storage_paths": [directory]}))
        cfg = {"collection_id": "fixed", "limits_hash": "same"}
        with patch.object(watch, "recovery_worker_active", return_value=True), patch.object(watch, "command", return_value="{}") as command:
            watch.guard(cfg, "resume")
        self.assertEqual(command.call_args.args[1][2], "biomed-web")
        self.assertEqual(command.call_args.args[1][-2:], ["--limits-hash", "same"])

    def test_handoff_recognizes_a_real_process_waiting_for_the_same_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / ".curation-worker-heartbeat.lock"
            with lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                waiter = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX)", str(lock_path)])
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not watch.waiting_for_worker_lock(root, waiter.pid):
                        time.sleep(0.05)
                    self.assertTrue(watch.waiting_for_worker_lock(root, waiter.pid))
                    self.assertFalse(watch.waiting_for_worker_lock(root, os.getpid()))
                finally:
                    waiter.terminate()
                    waiter.wait(timeout=5)

    def test_handoff_requires_original_process_and_waiter_before_stopping_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".curation-worker-recovery.pid").write_text("123")
            cfg = {"storage_paths": [directory]}
            info = f"true|456|{directory}"
            with (patch.object(watch, "recovery_worker_active", return_value=True),
                  patch.object(watch, "original_worker_matches", return_value=True),
                  patch.object(watch, "waiting_for_worker_lock", return_value=False) as waiter,
                  patch.object(watch, "command", return_value=info) as command):
                self.assertEqual(watch.handoff_recovery_worker(cfg), "waiting_for_original")
                self.assertEqual(command.call_count, 1)
                waiter.return_value = True
                self.assertEqual(watch.handoff_recovery_worker(cfg), "signal_sent")
                self.assertEqual(command.call_args.args[1][:3], ["docker", "exec", "biomed-web"])

    def test_handoff_rejects_different_database_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".curation-worker-recovery.pid").write_text("123")
            cfg = {"storage_paths": [directory]}
            with (patch.object(watch, "recovery_worker_active", return_value=True),
                  patch.object(watch, "command", return_value="true|456|/another/database") as command):
                self.assertEqual(watch.handoff_recovery_worker(cfg), "waiting_for_original")
                self.assertEqual(command.call_count, 1)

    def test_restart_never_restarts_annotation_worker_or_shared_docker(self):
        cfg, state = {}, {}
        with patch.object(watch, "save_state"), patch.object(watch, "command") as command:
            watch.act(cfg, state, "restart", 100)
        command.assert_called_once_with(
            cfg, ["docker", "restart", "-t", "30", "biomed-worker"], timeout=90
        )
        self.assertEqual(state["actions"], [{"kind": "restart", "at": 100}])

    def status(self, **kwargs):
        return {
            "limits_hash": "same",
            "state": "running",
            "saved": 133207,
            "completed_at": None,
            "alerts": [],
            "message": "",
            "recoverable_pause": False,
            "retry_not_before": None,
            "heartbeat": "2026-10-22T09:00:00+00:00",
            **kwargs,
        }

    def evaluate(self, status, state=None):
        now = watch.stamp("2026-10-22T09:00:01+00:00")
        with (
            patch.object(watch, "act") as act,
            patch.object(watch, "notify"),
            patch.object(watch, "halt"),
        ):
            result = watch.evaluate(
                {"limits_hash": "same"}, state if state is not None else {}, status, now
            )
        return result, act

    def test_user_pause_never_restarts(self):
        result, act = self.evaluate(
            self.status(state="paused", message="用户暂停；测试")
        )
        self.assertIsNone(result)
        act.assert_not_called()

    def test_retry_after_is_respected_even_after_failure_limit(self):
        result, act = self.evaluate(
            self.status(
                state="paused",
                recoverable_pause=True,
                retry_not_before="2026-10-22T12:00:00+00:00",
            )
        )
        self.assertIsNone(result)
        act.assert_not_called()

    def test_only_known_recoverable_pause_is_resumed(self):
        result, act = self.evaluate(self.status(state="paused", recoverable_pause=True))
        self.assertIsNone(result)
        self.assertEqual(act.call_args.args[2], "resume")
        result, act = self.evaluate(
            self.status(state="paused", message="PubMed PMID 集合不一致")
        )
        self.assertIn("不能安全", result)
        act.assert_not_called()

    def test_recovery_is_bounded_and_limits_never_overwritten(self):
        now = watch.stamp("2026-10-22T09:00:01+00:00")
        state = {"actions": [{"kind": "resume", "at": now - 10} for _ in range(3)]}
        result, act = self.evaluate(
            self.status(state="paused", recoverable_pause=True), state
        )
        self.assertIn("3 轮", result)
        act.assert_not_called()
        result, act = self.evaluate(self.status(limits_hash="changed"))
        self.assertIn("配置已改变", result)
        act.assert_not_called()

    def test_stale_worker_restarts_but_active_backoff_does_not(self):
        result, act = self.evaluate(self.status(heartbeat="2026-10-22T08:00:00+00:00"))
        self.assertIsNone(result)
        self.assertEqual(act.call_args.args[2], "restart")
        result, act = self.evaluate(
            self.status(heartbeat=None, retry_not_before="2026-10-22T12:00:00+00:00")
        )
        self.assertIsNone(result)
        act.assert_not_called()

    def test_long_cooldown_does_not_trigger_restart_when_it_ends(self):
        state = {"last_saved": 133207, "last_progress": 0}
        result, act = self.evaluate(
            self.status(retry_not_before="2026-10-22T09:00:00+00:00"), state
        )
        self.assertEqual(act.call_args.args[2], "restart")
        state["last_progress"] = 0
        self.evaluate(self.status(retry_not_before="2026-10-22T12:00:00+00:00"), state)
        result, act = self.evaluate(self.status(), state)
        self.assertIsNone(result)
        act.assert_not_called()

    def test_completed_collection_becomes_quiet_after_mail_is_accepted(self):
        state = {"notices": {"completed:same": {"sent_at": 100}}}
        result, act = self.evaluate(
            self.status(
                completed_at="2026-10-22T08:59:00Z",
                alerts=[{"reason": "collection_completed", "email_state": "sent"}],
            ),
            state,
        )
        self.assertIsNone(result)
        self.assertTrue(state["finished"])
        act.assert_not_called()

    def test_fulltext_progress_continues_after_metadata_completion(self):
        now = watch.stamp("2026-10-22T09:00:01+00:00")
        state = {
            "last_saved": 133207,
            "last_progress": now - 1800,
            "last_progress_token": ["fulltext", 10],
            "finished": True,
        }
        result, act = self.evaluate(
            self.status(
                completed_at="2026-10-21T08:59:00Z",
                monitoring_complete=False,
                progress_token=["fulltext", 11],
            ),
            state,
        )
        self.assertIsNone(result)
        self.assertNotIn("finished", state)
        self.assertEqual(state["last_progress"], now)
        act.assert_not_called()

    def test_fulltext_stall_and_source_wait_are_not_hidden_by_metadata_completion(self):
        now = watch.stamp("2026-10-22T09:00:01+00:00")
        state = {
            "last_progress": now - 1800,
            "last_progress_token": ["fulltext", 10],
        }
        status = self.status(
            completed_at="2026-10-21T08:59:00Z",
            monitoring_complete=False,
            progress_token=["fulltext", 10],
        )
        result, act = self.evaluate(status, state)
        self.assertIsNone(result)
        self.assertEqual(act.call_args.args[2], "restart")
        state["last_progress"] = now - 1800
        result, act = self.evaluate(
            status | {"retry_not_before": "2026-10-23T02:00:00Z", "heartbeat": None},
            state,
        )
        self.assertIsNone(result)
        self.assertEqual(state["last_progress"], now)
        act.assert_not_called()

    def test_old_metadata_notification_does_not_suppress_fulltext_completion(self):
        state = {"notices": {"completed": {"sent_at": 100}}}
        status = self.status(
            saved=None,
            completed_at="2026-10-21T08:59:00Z",
            monitoring_complete=True,
            monitoring_completed_at="2026-10-22T08:50:00Z",
            alerts=[{"reason": "collection_completed", "email_state": "sent"}],
        )
        with patch.object(watch, "notify") as notify, patch.object(watch, "act") as act:
            watch.evaluate({"limits_hash": "same"}, state, status,
                           watch.stamp("2026-10-22T09:00:01Z"))
        self.assertEqual(notify.call_args.args[2], "completed:same")
        self.assertNotIn("None", notify.call_args.args[4])
        act.assert_not_called()

    def test_unreadable_storage_stops_worker_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_file": str(Path(directory) / "state.json")}
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(cfg))
            Path(cfg["state_file"]).write_text('{"probe_failures": 2}')
            with (
                patch("sys.argv", ["watch", "--config", str(config)]),
                patch.object(watch, "inspect_storage", side_effect=PermissionError()),
                patch.object(watch, "command") as command,
                patch.object(watch, "act") as act,
                patch.object(watch, "notify") as notify,
            ):
                watch.main()
            self.assertEqual(command.call_args.args[1],
                             ["docker", "stop", "-t", "30", "biomed-worker"])
            act.assert_not_called()
            notify.assert_called_once()
            self.assertIn(
                "存储无法核验",
                json.loads(Path(cfg["state_file"]).read_text())["attention"],
            )

    def test_terminal_halt_survives_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_file": str(Path(directory) / "state.json")}
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(cfg))
            with (
                patch.object(watch, "guard", side_effect=RuntimeError()),
                patch.object(watch, "command") as command,
            ):
                watch.halt(cfg, {}, "自动恢复耗尽")
            self.assertEqual(command.call_args.args[1],
                             ["docker", "stop", "-t", "30", "biomed-worker"])
            with (
                patch("sys.argv", ["watch", "--config", str(config)]),
                patch.object(watch, "inspect_storage") as storage,
                patch.object(watch, "docker_startup", return_value=None),
                patch.object(watch, "act") as act,
                patch.object(watch, "notify"),
            ):
                watch.main()
            storage.assert_not_called()
            act.assert_not_called()

    def test_mail_config_failure_is_persisted_without_exposing_content(self):
        state = {}
        with patch.object(Path, "read_text", side_effect=PermissionError("secret")):
            watch.notify(
                {"mail_config": "/missing"}, state, "failure", "subject", "body", 100
            )
        self.assertEqual(state["notices"]["failure"]["error"], "PermissionError")
        self.assertNotIn("secret", json.dumps(state))


class GuardPauseTests(TestCase):
    def test_status_does_not_scan_entries_and_fulltext_remains_monitored(self):
        run = collections.create_collection("2021-10-15", "2026-10-15")
        run.state, run.phase = "running", "fulltext"
        run.estimate = {"acquisition": {"baseline_completed_at": "2026-10-21T08:00:00Z"}}
        run.save()
        output = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("literature_guard", str(run.pk), stdout=output)
        status = json.loads(output.getvalue())
        self.assertIsNone(status["saved"])
        self.assertFalse(status["monitoring_complete"])
        self.assertEqual(status["progress_token"], ["fulltext", run.step])
        self.assertFalse(any("curation_collectionentry" in q["sql"] for q in queries))

    def test_completed_baseline_can_report_frozen_exact_count(self):
        run = collections.create_collection("2021-10-15", "2026-10-15")
        run.state, run.phase = "paused", "metadata"
        run.message = "本范围候选元数据已扫描完成；全文按单独预算处理"
        run.estimate = {"acquisition": {
            "baseline_completed_at": "2026-10-21T08:00:00Z", "baseline_saved": 133207,
        }}
        run.save()
        output = StringIO()
        call_command("literature_guard", str(run.pk), stdout=output)
        status = json.loads(output.getvalue())
        self.assertEqual(status["saved"], 133207)
        self.assertTrue(status["monitoring_complete"])

    def test_manual_pause_during_retry_exhaustion_blocks_guard_resume(self):
        run = collections.create_collection("2021-10-15", "2026-10-15")
        run.state, run.message = "paused", "连续失败，已暂停"
        run.save()
        alert_pause(run, "retry_exhausted")
        self.assertTrue(recoverable_pause(run))
        run = collections.control_collection(run.pk, "pause")
        self.assertTrue(run.message.startswith("用户暂停"))
        self.assertFalse(recoverable_pause(run))
        with self.assertRaisesMessage(CommandError, "不自动恢复"):
            call_command(
                "literature_guard",
                str(run.pk),
                action="resume",
                limits_hash=limits_hash(run.limits),
            )
        run.refresh_from_db()
        self.assertEqual(run.state, "paused")
