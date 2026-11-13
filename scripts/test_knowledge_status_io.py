import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.knowledge_worker import write_status


class KnowledgeStatusIOTests(unittest.TestCase):
    def test_status_is_atomic_readable_and_does_not_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            report = {"activity": "running", "batches": [{"name": "实体瘤"}]}
            with patch(
                "scripts.knowledge_worker.os.fsync",
                side_effect=AssertionError("status must not force disk sync"),
            ):
                write_status(path, report)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_failed_replace_keeps_previous_status_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            path.write_text('{"activity":"previous"}')
            with (
                patch(
                    "scripts.knowledge_worker.os.replace",
                    side_effect=OSError("fixture"),
                ),
                self.assertRaises(OSError),
            ):
                write_status(path, {"activity": "running"})
            self.assertEqual(json.loads(path.read_text())["activity"], "previous")
            self.assertEqual(list(Path(folder).iterdir()), [path])
