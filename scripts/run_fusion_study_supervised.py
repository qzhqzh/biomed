"""Resume one isolated study after a guarded, paced model prefetch."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = Path('/appdata/02_resource/apps/biomed/research/artifacts/fusion-independent-20261113-v1/guarded-run-status.json')
RETRYABLE_REASONS = {
    'Another process began using GPU 3; preserve checkpoints',
    'Cannot verify GPU ownership; preserve checkpoints',
    'Sustained disk pressure; preserve checkpoints',
    'High memory pressure; preserve checkpoints',
    'Cannot verify memory pressure; preserve checkpoints',
    'Paced model loader preflight timed out; preserve checkpoints',
    'GPU 3 already has a compute process',
    'Shared system resources busy; preserve checkpoints',
    'Cannot verify shared system resources; preserve checkpoints',
}


def memory_state():
    lines = Path('/proc/meminfo').read_text().splitlines()
    available = int(next(row.split()[1] for row in lines if row.startswith('MemAvailable:'))) / 1024**2
    full = next(row for row in Path('/proc/pressure/memory').read_text().splitlines() if row.startswith('full '))
    average = float(dict(part.split('=') for part in full.split()[1:])['avg10'])
    return available, average


def memory_limit(available_gib, full_avg10, consecutive):
    """Avoid exhausting shared memory, including when swap is already full."""
    count = consecutive + 1 if available_gib < 6 and full_avg10 >= 2 else 0
    return available_gib < 2 or count >= 3, count


def verify_loader(command, *, cwd, env, log):
    """Only a transient timeout may retry; broken configurations still fail."""
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return {'state': 'paused', 'reason': 'Paced model loader preflight timed out; preserve checkpoints'}
    except OSError:
        return {'state': 'failed', 'reason': 'Paced model loader preflight could not start'}
    if result.returncode:
        return {'state': 'failed', 'reason': 'Paced model loader preflight failed'}
    return None


def resource_gate(available):
    """Wait before loading models when the shared host is already busy."""
    try:
        ready = available()
    except (OSError, ValueError, KeyError, StopIteration):
        return {'state': 'paused', 'reason': 'Cannot verify shared system resources; preserve checkpoints'}
    if not ready:
        return {'state': 'paused', 'reason': 'Shared system resources busy; preserve checkpoints'}
    return None


def run():
    for attempt in range(3):
        # Per-tensor paced loading avoids relying on a large retained cache.
        if os.environ.get('BIOMED_PACED_MODEL_IO') != '1':
            prefetch = subprocess.run([sys.executable, '-B', 'scripts/prefetch_fusion_holdout.py'], cwd=ROOT, check=False)
            if prefetch.returncode:
                return prefetch.returncode
        result = subprocess.run([sys.executable, '-B', 'scripts/run_fusion_holdout_guarded.py'], cwd=ROOT, check=False)
        if not result.returncode:
            return 0
        state = json.loads(STATUS.read_text(encoding='utf-8')) if STATUS.exists() else {}
        if state.get('state') not in {'paused', 'waiting'} or state.get('reason') not in RETRYABLE_REASONS or attempt == 2:
            return result.returncode
        print('Resource conflict; preserve checkpoints and retry after a short wait.', flush=True)
        for _ in range(4):
            time.sleep(30)
    return 1


if __name__ == '__main__':
    raise SystemExit(run())
