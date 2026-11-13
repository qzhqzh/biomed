"""Pace read-only prewarming of existing 9B weights for one guarded study.

No model files are copied or changed. Pause on shared-host pressure and leave
the existing nightly worker its GPU. Cache residency is observed via mincore.
"""

import ctypes
import datetime
import json
import os
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL = Path('/data/models/Qwen3.5-9B')
ROOT = Path('/appdata/02_resource/apps/biomed/research/artifacts/fusion-independent-20261113-v1')
STATUS = ROOT / 'prefetch-status.json'
GPU = 'GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2'
RATE = 8 * 1024 * 1024
CHUNK = 1024 * 1024
PAGE = os.sysconf('SC_PAGE_SIZE')

lib = ctypes.CDLL(None, use_errno=True)
lib.mmap.restype = ctypes.c_void_p
lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
lib.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
lib.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def resident(fd, size):
    address = lib.mmap(None, size, 0, 1, fd, 0)
    if address == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_errno(), 'Cannot inspect model file cache')
    try:
        vector = (ctypes.c_ubyte * ((size + PAGE - 1) // PAGE))()
        if lib.mincore(address, size, vector):
            raise OSError(ctypes.get_errno(), 'Cannot inspect model file cache')
        return vector
    finally:
        lib.munmap(address, size)


def pressure():
    def average(file, row, key):
        value = next(s for s in Path(file).read_text().splitlines() if s.startswith(row + ' '))
        return float(dict(part.split('=') for part in value.split()[1:])[key])

    memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    return {'io_full_avg10': average('/proc/pressure/io', 'full', 'avg10'),
            'memory_some_avg10': average('/proc/pressure/memory', 'some', 'avg10'),
            'available_gib': round(memory['MemAvailable'] / 1024**2, 2)}


def gpu_in_use():
    output = subprocess.check_output(['nvidia-smi', '-i', GPU, '--query-compute-apps=pid',
                                      '--format=csv,noheader,nounits'], text=True, timeout=8)
    return any(line.strip().isdigit() for line in output.splitlines())


def permitted_window():
    local = datetime.datetime.now(ZoneInfo('Asia/Shanghai'))
    return local.hour >= 8 and (local.hour != 23 or local.minute < 55)


def save(state, **values):
    result = {'updated_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'state': state,
              'read_rate_limit_mib_s': RATE / 1024**2, 'model_files_modified': False,
              'target_written': False, **values}
    temp = STATUS.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    temp.replace(STATUS)


def run():
    paths = sorted(MODEL.glob('*.safetensors'))
    if len(paths) != 4 or any(not path.is_file() for path in paths):
        raise ValueError('Expected the four existing 9B weight shards')
    total, read_bytes, skipped, waits = sum(p.stat().st_size for p in paths), 0, 0, 0
    started, last_report, monitor_next = time.monotonic(), 0, 0
    for path in paths:
        with path.open('rb', buffering=0) as stream:
            fd, size = stream.fileno(), path.stat().st_size
            vector = resident(fd, size)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_RANDOM)
            for offset in range(0, size, CHUNK):
                length = min(CHUNK, size - offset)
                section = vector[offset // PAGE:(offset + length + PAGE - 1) // PAGE]
                if section and all(value & 1 for value in section):
                    skipped += length
                    continue
                if not permitted_window():
                    save('deferred', reason='Yielding before the scheduled nightly worker', bytes_read=read_bytes)
                    return False
                if time.monotonic() >= monitor_next:
                    while True:
                        p = pressure()
                        if not permitted_window():
                            save('deferred', reason='Yielding before the scheduled nightly worker', bytes_read=read_bytes)
                            return False
                        if not gpu_in_use() and p['io_full_avg10'] < 4 and p['memory_some_avg10'] < 1 and p['available_gib'] >= 16:
                            break
                        waits += 1
                        save('waiting_resources', bytes_read=read_bytes, bytes_cached_skipped=skipped,
                             total_bytes=total, pressure=p, wait_count=waits)
                        time.sleep(10)
                    monitor_next = time.monotonic() + 5
                os.lseek(fd, offset, os.SEEK_SET)
                before = time.monotonic()
                data = os.read(fd, length)
                if len(data) != length:
                    raise OSError('Unexpected short model read')
                read_bytes += len(data)
                time.sleep(max(0, len(data) / RATE - (time.monotonic() - before)))
                if time.monotonic() - last_report >= 30:
                    save('warming', current_file=path.name, bytes_read=read_bytes, bytes_cached_skipped=skipped,
                         total_bytes=total, pressure=pressure(), wait_count=waits)
                    last_report = time.monotonic()
    percentages, cached_bytes = {}, 0
    for path in paths:
        with path.open('rb', buffering=0) as stream:
            size = path.stat().st_size
            vector = resident(stream.fileno(), size)
            fraction = sum(value & 1 for value in vector) / len(vector)
            percentages[path.name] = round(fraction * 100, 1)
            cached_bytes += int(size * fraction)
    cached_percent = round(100 * cached_bytes / total, 1)
    ready = cached_percent >= 90 and permitted_window()
    save('ready' if ready else 'deferred', reason='' if ready else 'Insufficient cache or outside study window',
         bytes_read=read_bytes, bytes_cached_skipped=skipped, total_bytes=total, cache_percent=cached_percent,
         per_file_cache_percent=percentages, elapsed_seconds=round(time.monotonic() - started, 1), wait_count=waits)
    return ready


if __name__ == '__main__':
    try:
        ok = run()
    except Exception as exc:
        save('failed', reason=type(exc).__name__)
        raise
    raise SystemExit(0 if ok else 2)
