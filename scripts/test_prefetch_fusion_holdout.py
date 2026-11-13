"""Safety checks for the paced, read-only model-cache warmer."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prefetch_fusion_holdout as prefetch


class PrefetchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = self.root / 'model'
        self.model.mkdir()
        for number in range(4):
            path = self.model / f'shard-{number}.safetensors'
            with path.open('wb') as stream:
                stream.truncate(8192)
        self.status = self.root / 'status.json'
        for name, value in (('MODEL', self.model), ('STATUS', self.status), ('CHUNK', 4096), ('RATE', 10**10)):
            patcher = patch.object(prefetch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.normal = {'io_full_avg10': 0, 'memory_some_avg10': 0, 'available_gib': 24}

    def test_reads_existing_files_without_altering_them(self):
        before = {p.name: p.read_bytes() for p in self.model.iterdir()}
        with patch.object(prefetch, 'pressure', return_value=self.normal), \
             patch.object(prefetch, 'gpu_in_use', return_value=False), \
             patch.object(prefetch, 'permitted_window', return_value=True), \
             patch.object(prefetch.time, 'sleep'):
            self.assertTrue(prefetch.run())
        after = {p.name: p.read_bytes() for p in self.model.iterdir()}
        self.assertEqual(before, after)
        status = json.loads(self.status.read_text())
        self.assertEqual(status['state'], 'ready')
        self.assertFalse(status['model_files_modified'])

    def test_outside_window_never_loads_model(self):
        # Explicitly evict only the tiny synthetic fixture from the page cache.
        for path in self.model.iterdir():
            with path.open('rb') as stream:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        with patch.object(prefetch, 'pressure', return_value=self.normal), \
             patch.object(prefetch, 'gpu_in_use', return_value=False), \
             patch.object(prefetch, 'permitted_window', return_value=False), \
             patch.object(prefetch.time, 'sleep'):
            self.assertFalse(prefetch.run())
        self.assertEqual(json.loads(self.status.read_text())['state'], 'deferred')


if __name__ == '__main__':
    unittest.main()
