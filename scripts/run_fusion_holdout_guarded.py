import datetime
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from paced_safetensors import system_available
from prefetch_fusion_holdout import permitted_window
from run_fusion_study_supervised import (
    memory_limit,
    memory_state,
    resource_gate,
    verify_loader,
)

root = Path('/appdata/02_resource/apps/biomed/research/artifacts/fusion-independent-20261113-v1')
work = Path('/data/06_project/biomed')
gpu = 'GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2'
status = root / 'guarded-run-status.json'
log_path = root / 'guarded-run.log'
command = [str(work / '.venv/bin/python'), '-B', 'scripts/run_literature_batch.py',
           '--root', str(root / 'new-batch'), '--snapshot', str(root / 'new-source-snapshot.json'),
           '--selection-file', str(root / 'new-pmids.json'),
           '--dictionary', str(work / 'var/experiments/target-compatibility-20261101/target-dictionary.json'),
           '--clinical-profile', 'target-clinical-focus-v1']
module_paths = [str(work)]
if os.environ.get('BIOMED_PACED_MODEL_IO') == '1':
    module_paths.insert(0, str(work / 'scripts/paced_model_site'))
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join(module_paths), DJANGO_DEBUG='1',
           DATABASE_PATH='/tmp/biomed-fusion-holdout-no-production.sqlite3',
           CURATION_STORAGE_DIR='/tmp/biomed-fusion-holdout-library')

def owners():
    result = subprocess.check_output(['nvidia-smi', '-i', gpu, '--query-compute-apps=pid',
                                      '--format=csv,noheader,nounits'], text=True, timeout=8)
    return {int(line.strip()) for line in result.splitlines() if line.strip().isdigit()}

def descendant(pid, ancestor):
    seen = set()
    for _ in range(80):
        if pid == ancestor:
            return True
        if pid <= 1 or pid in seen:
            return False
        seen.add(pid)
        try:
            pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False

def save(**data):
    paced = os.environ.get('BIOMED_PACED_MODEL_IO') == '1'
    payload = {'updated_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'gpu': gpu, 'target_written': False, 'production_import': False,
               'io_read_strategy': 'per_tensor_8_mib_s' if paced else 'unmodified_loader',
               'paced_reader_sha256': hashlib.sha256((work / 'scripts/paced_safetensors.py').read_bytes()).hexdigest()
               if paced else None, **data}
    temp = status.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    temp.replace(status)

def finish(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def compare_completed_batch(log):
    """Add structural comparison after extraction; never imply semantic review."""
    output = root / 'new-candidates-comparison.json'
    knowledge = root / 'new-batch/final/knowledge.json'
    export = root / 'new-batch/final/tumor-api-export.json'
    if not output.exists():
        command = [sys.executable, '-B', 'scripts/evaluate_fusion_independent_new.py',
                   '--root', str(root), '--output', str(output)]
        try:
            result = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return 'failed'
        if result.returncode:
            return 'failed'
    try:
        existing = json.loads(output.read_text(encoding='utf-8'))
        hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for name, path in [('knowledge_sha256', knowledge), ('export_sha256', export)]}
        if (any(existing.get(name) != value for name, value in hashes.items())
                or existing.get('target_written') or existing.get('production_written')):
            return 'failed'
    except (OSError, ValueError, TypeError):
        return 'failed'
    return 'completed_needs_source_review'


if not permitted_window():
    save(state='paused', reason='Yielding GPU before the scheduled nightly worker')
    raise SystemExit('Outside the permitted study window')
try:
    busy = owners()
except (OSError, subprocess.SubprocessError):
    save(state='paused', reason='Cannot verify GPU ownership; preserve checkpoints')
    raise SystemExit('Cannot verify GPU ownership') from None
if busy:
    save(state='waiting', reason='GPU 3 already has a compute process')
    raise SystemExit('GPU 3 already in use')
if os.environ.get('BIOMED_PACED_MODEL_IO') == '1':
    failure = resource_gate(system_available)
    if failure:
        save(**failure)
        raise SystemExit(failure['reason'])
with log_path.open('a', encoding='utf-8') as log:
    if os.environ.get('BIOMED_PACED_MODEL_IO') == '1':
        verify_env = dict(env, BIOMED_PACED_VERIFY='1')
        failure = verify_loader(['/appdata/02_resource/apps/biomed/research/runtime/bin/python', '-B', '-c',
                                 'from scripts.paced_safetensors import require_installed; require_installed()'],
                                cwd=work, env=verify_env, log=log)
        if failure:
            save(**failure)
            raise SystemExit(failure['reason'])
    process = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    save(state='running', pid=process.pid, reason='')
    pressure_count = 0
    monitor_failures = 0
    memory_count = 0
    memory_monitor_failures = 0
    try:
        while process.poll() is None:
            time.sleep(10)
            if process.poll() is not None:
                break
            try:
                other = [pid for pid in owners() if not descendant(pid, process.pid)]
                monitor_failures = 0
                if other:
                    save(state='paused', reason='Another process began using GPU 3; preserve checkpoints')
                    finish(process)
                    raise SystemExit('Stopped the experiment for another GPU user')
            except (OSError, subprocess.SubprocessError):
                monitor_failures += 1
                if monitor_failures >= 3:
                    save(state='paused', reason='Cannot verify GPU ownership; preserve checkpoints')
                    finish(process)
                    raise SystemExit('Stopped because GPU monitoring was unavailable')
            try:
                available, memory_full = memory_state()
                stop_for_memory, memory_count = memory_limit(available, memory_full, memory_count)
                memory_monitor_failures = 0
            except (OSError, ValueError, KeyError, StopIteration):
                memory_monitor_failures += 1
                if memory_monitor_failures >= 3:
                    save(state='paused', reason='Cannot verify memory pressure; preserve checkpoints')
                    finish(process)
                    raise SystemExit('Stopped because memory monitoring was unavailable')
                stop_for_memory = False
            if stop_for_memory:
                save(state='paused', reason='High memory pressure; preserve checkpoints',
                     available_gib=round(available, 2), memory_full_avg10=memory_full)
                finish(process)
                raise SystemExit('Stopped for shared memory pressure')
            rows = Path('/proc/pressure/io').read_text().splitlines()
            full = next(row for row in rows if row.startswith('full '))
            metrics = dict(part.split('=') for part in full.split()[1:])
            pressure_count = pressure_count + 1 if float(metrics['avg60']) >= 20 else 0
            if pressure_count >= 6:
                save(state='paused', reason='Sustained disk pressure; preserve checkpoints')
                finish(process)
                raise SystemExit('Stopped for sustained disk pressure')
            local = datetime.datetime.now(ZoneInfo('Asia/Shanghai'))
            if local.hour == 23 and local.minute >= 55:
                save(state='paused', reason='Yielding GPU before the scheduled nightly worker')
                finish(process)
                raise SystemExit('Yielded to scheduled nightly service')
            save(state='running', pid=process.pid, reason='')
    except BaseException:
        finish(process)
        raise
    code = process.returncode
    comparison = compare_completed_batch(log) if code == 0 else 'not_started'
    save(state='completed' if code == 0 else 'failed', returncode=code,
         comparison_state=comparison,
         reason='' if code == 0 and comparison != 'failed' else
         'Inspect guarded-run.log and preserved stage logs')
    print(json.dumps({'state': 'completed' if code == 0 else 'failed', 'returncode': code, 'log': str(log_path)}, ensure_ascii=False))
    raise SystemExit(code)
