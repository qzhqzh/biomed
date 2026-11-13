import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.curation.management.commands import curation_worker
from apps.curation.views import overview
from apps.curation.worker_heartbeat import (
    single_worker_lock,
    touch_worker_heartbeat,
    worker_last_seen,
)


class WorkerHeartbeatFileTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "heartbeat"
        settings = override_settings(CURATION_HEARTBEAT_FILE=str(self.path))
        settings.enable()
        self.addCleanup(settings.disable)

    def test_fresh_heartbeat_avoids_database_write_and_fresh_read_avoids_query(self):
        with (
            patch.object(curation_worker, "_last_heartbeat", 0),
            patch.object(curation_worker.time, "monotonic", return_value=100),
            patch.object(curation_worker.WorkerHeartbeat.objects, "filter") as query,
        ):
            self.assertTrue(curation_worker.heartbeat())
            query.return_value.exists.assert_called_once()
            query.return_value.update.assert_not_called()
        model = MagicMock()
        self.assertLess((timezone.now() - worker_last_seen(model)).total_seconds(), 2)
        model.objects.filter.assert_not_called()
        with patch(
            "apps.curation.views.WorkerHeartbeat.objects.filter",
            side_effect=AssertionError("unexpected database read"),
        ):
            response = overview(RequestFactory().get("/?runtime=1"))
        self.assertTrue(json.loads(response.content)["worker_online"])

    def test_file_failure_falls_back_to_existing_database_heartbeat(self):
        with (
            patch.object(curation_worker, "_last_heartbeat", 0),
            patch.object(curation_worker.time, "monotonic", return_value=100),
            patch.object(curation_worker, "touch_worker_heartbeat", return_value=False),
            patch.object(curation_worker.WorkerHeartbeat.objects, "filter") as query,
        ):
            query.return_value.update.return_value = 1
            self.assertTrue(curation_worker.heartbeat())
            query.return_value.update.assert_called_once()

    def test_unreadable_database_does_not_emit_false_liveness(self):
        with (
            patch.object(curation_worker, "_last_heartbeat", 0),
            patch.object(curation_worker.time, "monotonic", return_value=100),
            patch.object(curation_worker.WorkerHeartbeat.objects, "filter") as query,
        ):
            query.return_value.exists.side_effect = OperationalError(
                "database is locked"
            )
            with self.assertRaises(OperationalError):
                curation_worker.heartbeat()
        self.assertFalse(self.path.exists())

    def test_stale_or_missing_file_uses_newer_database_record(self):
        self.path.touch()
        old = timezone.now() - timedelta(minutes=2)
        os.utime(self.path, (old.timestamp(), old.timestamp()))
        newer = timezone.now() - timedelta(seconds=5)
        model = MagicMock()
        model.objects.filter.return_value.first.return_value = SimpleNamespace(
            last_seen=newer
        )
        self.assertEqual(worker_last_seen(model), newer)
        self.path.unlink()
        self.assertEqual(worker_last_seen(model), newer)

    def test_fallback_does_not_hide_database_corruption(self):
        self.path.touch()
        old = timezone.now() - timedelta(minutes=2)
        os.utime(self.path, (old.timestamp(), old.timestamp()))
        model = MagicMock()
        model.objects.filter.side_effect = OperationalError(
            "database disk image is malformed"
        )
        with self.assertRaises(OperationalError):
            worker_last_seen(model)

    def test_symlink_and_future_timestamp_do_not_create_false_liveness(self):
        target = Path(self.temp.name) / "target"
        target.touch()
        self.path.symlink_to(target)
        self.assertFalse(touch_worker_heartbeat())
        model = MagicMock()
        model.objects.filter.return_value.first.return_value = None
        self.assertIsNone(worker_last_seen(model))
        self.path.unlink()
        self.path.touch()
        future = timezone.now() + timedelta(minutes=5)
        os.utime(self.path, (future.timestamp(), future.timestamp()))
        self.assertIsNone(worker_last_seen(model))

    def test_health_check_accepts_fresh_and_rejects_stale_signal(self):
        self.assertTrue(touch_worker_heartbeat())
        call_command("curation_worker_health")
        old = timezone.now() - timedelta(minutes=2)
        os.utime(self.path, (old.timestamp(), old.timestamp()))
        with patch(
            "apps.curation.management.commands.curation_worker_health.WorkerHeartbeat.objects.filter"
        ) as query:
            query.return_value.first.return_value = None
            with self.assertRaisesMessage(CommandError, "heartbeat is stale"):
                call_command("curation_worker_health")

    def test_only_one_worker_can_hold_the_deployment_lock(self):
        code = (
            "import fcntl, os, sys; "
            "f=os.open(sys.argv[1], os.O_WRONLY); "
            "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
        )
        lock = str(self.path) + ".lock"
        with single_worker_lock():
            blocked = subprocess.run(
                [sys.executable, "-c", code, lock], capture_output=True, check=False
            )
            self.assertNotEqual(blocked.returncode, 0)
        acquired = subprocess.run(
            [sys.executable, "-c", code, lock], capture_output=True, check=False
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
