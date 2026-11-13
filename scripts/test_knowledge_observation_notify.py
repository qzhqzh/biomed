import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.curation.pubtator import read
from scripts.knowledge_observation_notify import deliver


class ObservationNotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.observation = self.root / "observation"
        self.observation.mkdir()
        (self.observation / "report.md").write_text("Observation fixture", encoding="utf-8")

    def test_preview_never_sends_email(self):
        with patch("scripts.knowledge_observation_notify.subprocess.run"), patch("scripts.knowledge_observation_notify.notify") as mail:
            self.assertEqual(deliver(self.root, self.observation, "unused"), "preview_only")
        mail.assert_not_called()

    def test_success_is_durable_and_idempotent(self):
        def sent(cfg, state, key, subject, body, now):
            state.setdefault("notices", {})[key] = {"sent_at": now}

        with patch("scripts.knowledge_observation_notify.subprocess.run") as report, patch("scripts.knowledge_observation_notify.notify", side_effect=sent) as mail:
            self.assertEqual(deliver(self.root, self.observation, "unused", send=True), "accepted_by_mail_server")
            self.assertEqual(deliver(self.root, self.observation, "unused", send=True), "already_sent")
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(report.call_count, 1)

    def test_failure_preserves_retry_state_and_reports_failure(self):
        def failed(cfg, state, key, subject, body, now):
            state.setdefault("notices", {})[key] = {"next_attempt": now + 300}

        with (patch("scripts.knowledge_observation_notify.subprocess.run"),
              patch("scripts.knowledge_observation_notify.notify", side_effect=failed),
              self.assertRaisesRegex(RuntimeError, "not accepted")):
            deliver(self.root, self.observation, "unused", send=True)
        state = read(self.observation / "notification-state.json")
        self.assertIn("next_attempt", state["notices"]["observation-report"])
