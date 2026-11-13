import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.paced_safetensors import PacedSafeOpen, Reader


class Clock:
    def __init__(self):
        self.seconds = 0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class Slice:
    def __getitem__(self, item):
        return ('original', item)

    def get_shape(self):
        return [10]


class Inner:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def get_slice(self, name):
        return Slice()

    def get_tensor(self, name):
        return ('original_tensor', name)

    def keys(self):
        return ['tensor']

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class PacedSafetensorsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'fixture.safetensors'

    def save(self, offsets):
        header = json.dumps({'tensor': {'dtype': 'U8', 'shape': [10], 'data_offsets': offsets}}).encode()
        self.path.write_bytes(struct.pack('<Q', len(header)) + header + b'0123456789')

    def test_paces_only_tensor_range_and_waits_for_available_resources(self):
        self.save([0, 10])
        clock = Clock()
        states = iter((False, True, True))
        reader = Reader(self.path, rate=4, chunk=4, available=lambda: next(states),
                        sleep=clock.sleep, monotonic=clock.monotonic)
        self.addCleanup(reader.close)
        reader.read_tensor('tensor')
        self.assertEqual(clock.seconds, 7.5)  # 5 s resource wait + 10 bytes / 4 bytes/s.

    def test_wrapper_preserves_get_slice_and_get_tensor(self):
        self.save([0, 10])
        with patch('scripts.paced_safetensors.system_available', return_value=True), \
             PacedSafeOpen(Inner, self.path) as source:
            self.assertEqual(source.keys(), ['tensor'])
            self.assertEqual(source.get_slice('tensor').get_shape(), [10])
            self.assertEqual(source.get_slice('tensor')[...], ('original', Ellipsis))
            self.assertEqual(source.get_tensor('tensor'), ('original_tensor', 'tensor'))
            inner = source.inner
        self.assertTrue(inner.closed)
        self.assertIsNone(source.reader.fd)

    def test_rejects_offsets_outside_the_file(self):
        self.save([0, 11])
        with self.assertRaisesRegex(ValueError, 'offsets'):
            Reader(self.path)


if __name__ == '__main__':
    unittest.main()
