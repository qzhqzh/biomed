import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run_fusion_study_supervised as supervisor


class SupervisedStudyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.status = Path(self.temporary.name) / 'guard.json'
        patcher = patch.object(supervisor, 'STATUS', self.status)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resource_pause_retries_without_losing_checkpoints(self):
        self.status.write_text(json.dumps({'state': 'paused', 'reason': 'Sustained disk pressure; preserve checkpoints'}))
        exits = [SimpleNamespace(returncode=v) for v in (0, 1, 0, 0)]
        with patch.object(supervisor.subprocess, 'run', side_effect=exits) as run, \
             patch.object(supervisor.time, 'sleep') as sleep:
            self.assertEqual(supervisor.run(), 0)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(sleep.call_count, 4)

    def test_unknown_failure_or_closed_window_is_not_retried(self):
        self.status.write_text(json.dumps({'state': 'failed', 'reason': 'Invalid model output'}))
        with patch.object(supervisor.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0),
                                                                    SimpleNamespace(returncode=1)]) as run, \
             patch.object(supervisor.time, 'sleep') as sleep:
            self.assertEqual(supervisor.run(), 1)
        self.assertEqual(run.call_count, 2)
        sleep.assert_not_called()
        with patch.object(supervisor.subprocess, 'run', return_value=SimpleNamespace(returncode=2)) as run:
            self.assertEqual(supervisor.run(), 2)
        self.assertEqual(run.call_count, 1)

    def test_memory_guard_requires_sustained_pressure_or_critical_availability(self):
        stop, count = supervisor.memory_limit(5, 2.5, 0)
        self.assertFalse(stop)
        self.assertEqual(count, 1)
        stop, count = supervisor.memory_limit(5, 2.5, count)
        self.assertFalse(stop)
        stop, count = supervisor.memory_limit(5, 2.5, count)
        self.assertTrue(stop)
        self.assertEqual(count, 3)
        self.assertEqual(supervisor.memory_limit(7, 5, count), (False, 0))
        self.assertTrue(supervisor.memory_limit(1.9, 0, 0)[0])

    def test_paced_model_loader_does_not_run_whole_model_prefetch(self):
        with patch.dict(supervisor.os.environ, {'BIOMED_PACED_MODEL_IO': '1'}), \
             patch.object(supervisor.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(supervisor.run(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn('scripts/run_fusion_holdout_guarded.py', run.call_args.args[0])

    def test_preflight_timeout_is_retryable_but_actual_failure_is_not(self):
        timeout = subprocess.TimeoutExpired(['python'], 60)
        with patch.object(supervisor.subprocess, 'run', side_effect=timeout):
            state = supervisor.verify_loader(['python'], cwd='.', env={}, log=None)
        self.assertEqual(state['state'], 'paused')
        self.assertIn(state['reason'], supervisor.RETRYABLE_REASONS)
        with patch.object(supervisor.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            state = supervisor.verify_loader(['python'], cwd='.', env={}, log=None)
        self.assertEqual(state['state'], 'failed')
        self.assertNotIn(state['reason'], supervisor.RETRYABLE_REASONS)

    def test_busy_gpu_waits_and_retries_in_paced_mode(self):
        self.status.write_text(json.dumps({'state': 'waiting', 'reason': 'GPU 3 already has a compute process'}))
        exits = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
        with patch.dict(supervisor.os.environ, {'BIOMED_PACED_MODEL_IO': '1'}), \
             patch.object(supervisor.subprocess, 'run', side_effect=exits) as run, \
             patch.object(supervisor.time, 'sleep') as sleep:
            self.assertEqual(supervisor.run(), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleep.call_count, 4)

    def test_busy_or_unreadable_shared_resources_defer_before_model_load(self):
        for value in (False, OSError('unavailable')):
            def available(value=value):
                if isinstance(value, Exception):
                    raise value
                return value
            state = supervisor.resource_gate(available)
            self.assertEqual(state['state'], 'paused')
            self.assertIn(state['reason'], supervisor.RETRYABLE_REASONS)
        self.assertIsNone(supervisor.resource_gate(lambda: True))


if __name__ == '__main__':
    unittest.main()
