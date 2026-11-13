"""Read individual safetensors ranges at a bounded pace before mmap access.

Used only by the isolated fusion study.  It changes neither model weights nor
tensor values.  The caller must explicitly opt in and restrict the model path.
"""

import json
import os
import struct
import time
from pathlib import Path

RATE = 8 * 1024 * 1024
CHUNK = 1024 * 1024
MAX_HEADER = 32 * 1024 * 1024


def require_installed():
    """Fail closed if the experiment requested pacing but startup failed."""
    import safetensors
    from transformers import modeling_utils

    loader = safetensors.safe_open
    if not getattr(loader, 'biomed_paced', False) or modeling_utils.safe_open is not loader:
        raise RuntimeError('Study model I/O pacing was requested but not installed')


def system_available():
    memory = next(row for row in Path('/proc/meminfo').read_text().splitlines()
                  if row.startswith('MemAvailable:'))
    available = int(memory.split()[1]) / 1024**2
    io = next(row for row in Path('/proc/pressure/io').read_text().splitlines() if row.startswith('full '))
    memory_full = next(row for row in Path('/proc/pressure/memory').read_text().splitlines()
                       if row.startswith('full '))
    def avg(row):
        return float(dict(part.split('=') for part in row.split()[1:])['avg10'])
    return available >= 3 and avg(io) < 4 and avg(memory_full) < 2


class Reader:
    def __init__(self, path, *, rate=RATE, chunk=CHUNK, available=None,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.path = Path(path)
        self.rate, self.chunk = rate, chunk
        self.available, self.sleep, self.monotonic = available or system_available, sleep, monotonic
        if rate <= 0 or chunk <= 0:
            raise ValueError('Read rate and chunk must be positive')
        self.fd = os.open(self.path, os.O_RDONLY)
        try:
            if hasattr(os, 'posix_fadvise'):
                os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_RANDOM)
            size = os.fstat(self.fd).st_size
            prefix = os.pread(self.fd, 8, 0)
            if len(prefix) != 8:
                raise ValueError('Incomplete safetensors header')
            length = struct.unpack('<Q', prefix)[0]
            if length < 2 or length > MAX_HEADER or 8 + length > size:
                raise ValueError('Invalid safetensors header length')
            raw = os.pread(self.fd, length, 8)
            if len(raw) != length:
                raise ValueError('Incomplete safetensors metadata')
            header = json.loads(raw)
            if not isinstance(header, dict):
                raise TypeError('Invalid safetensors metadata')
            self.offset = 8 + length
            self.ranges = {}
            for key, value in header.items():
                if key == '__metadata__':
                    continue
                offsets = value.get('data_offsets') if isinstance(value, dict) else None
                if (not isinstance(offsets, list) or len(offsets) != 2
                        or any(type(number) is not int for number in offsets)
                        or not 0 <= offsets[0] <= offsets[1] <= size - self.offset):
                    raise ValueError('Invalid safetensors tensor offsets')
                self.ranges[key] = offsets
            self.next_check = 0.0
        except BaseException:
            self.close()
            raise

    def read_tensor(self, name):
        start, end = self.ranges[name]
        while start < end:
            now = self.monotonic()
            if now >= self.next_check:
                while not self.available():
                    self.sleep(5)
                self.next_check = self.monotonic() + 5
            count = min(self.chunk, end - start)
            before = self.monotonic()
            data = os.pread(self.fd, count, self.offset + start)
            if len(data) != count:
                raise OSError('Short model weight read')
            self.sleep(max(0, count / self.rate - (self.monotonic() - before)))
            start += count

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class PacedSlice:
    def __init__(self, reader, inner, name):
        self.reader, self.inner, self.name = reader, inner, name

    def __getitem__(self, item):
        self.reader.read_tensor(self.name)
        return self.inner[item]

    def __getattr__(self, name):
        return getattr(self.inner, name)


class PacedSafeOpen:
    def __init__(self, original, path, *args, **kwargs):
        self.reader = Reader(path)
        try:
            self.inner = original(path, *args, **kwargs)
        except BaseException:
            self.reader.close()
            raise

    def get_slice(self, name):
        return PacedSlice(self.reader, self.inner.get_slice(name), name)

    def get_tensor(self, name):
        self.reader.read_tensor(name)
        return self.inner.get_tensor(name)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.inner.__exit__(*args)
        finally:
            self.reader.close()

    def __getattr__(self, name):
        return getattr(self.inner, name)
