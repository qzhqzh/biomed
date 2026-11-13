"""Launch a no-network, read-only target fusion test against temporary SQLite."""

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.run_target_isolated_validation import HARNESS, TARGET, output, sha

PROJECT = Path(__file__).resolve().parents[1]
STORAGE = PROJECT / 'scripts/probe_target_memory_storage.py'
PROBE = PROJECT / 'scripts/probe_target_fusion_memory.py'
DEVELOPMENT = PROJECT / 'var/experiments/target-holdout-20261113-v1/fusion-development-v1.json'
OUTPUT = Path('/tmp/biomed-038-target-fusion-memory-v1.json')
CONTAINER_NAME = 'biomed-target-fusion-validation-038'


def main():
    if OUTPUT.exists():
        raise FileExistsError('Preserve first isolated result; use a new version')
    image = output(['docker', 'inspect', 'zhuqin-tumor-api', '--format', '{{.Image}}'])
    # Launch the immutable ID reported by the running container directly: this
    # is stronger than relying on a mutable tag and cannot trigger a pull.
    if not image.startswith('sha256:'):
        raise ValueError('Running target did not report an immutable image ID')
    mounts = [(TARGET, '/opt/api'),
              (Path('/home/zhuqin/project/zhenyuan/drf_somatic_biomeddb'), '/opt/drf_somatic_biomeddb'),
              (Path('/home/zhuqin/project/zhenyuan/drf_core'), '/opt/drf_core'),
              (Path('/home/zhuqin/project/zhenyuan/sdk_aliyun_oss'), '/opt/sdk_aliyun_oss'),
              (Path('/home/zhuqin/project/zhenyuan/sdk_event_bus'), '/opt/sdk_event_bus'),
              (HARNESS, '/test/probe_target_isolated_runtime.py'),
              (STORAGE, '/test/probe_target_memory_storage.py'), (PROBE, '/test/fusion.py'),
              (DEVELOPMENT.resolve(), '/test/development.json')]
    cmd = ['docker', 'run', '--name', CONTAINER_NAME, '--rm', '--pull=never', '--network=none', '--read-only', '--user', '1000:1000',
           '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=1g',
           '--cpus=0.5', '--tmpfs', '/tmp:size=128m']
    for source, destination in mounts:
        cmd.extend(('--mount', f'type=bind,src={source},dst={destination},readonly'))
    cmd.extend(('--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONWARNINGS=ignore',
                '--entrypoint', 'python', image, '-B', '/test/fusion.py'))
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=150, check=False)
    except subprocess.TimeoutExpired:
        # The fixed name belongs only to this short-lived, isolated test.
        try:
            subprocess.run(['docker', 'rm', '--force', CONTAINER_NAME], text=True, capture_output=True,
                           timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    if completed.returncode:
        raise RuntimeError(f'Isolated in-memory fusion test failed: {completed.stderr[-5000:]}')
    lines = [line.removeprefix('BIOMED_FUSION_RESULT=') for line in completed.stdout.splitlines()
             if line.startswith('BIOMED_FUSION_RESULT=')]
    if len(lines) != 1:
        raise ValueError('Expected exactly one isolated fusion result')
    result = json.loads(lines[0])
    result.update({'running_image_id': image, 'development_sha256': sha(DEVELOPMENT),
                   'probe_sha256': sha(PROBE), 'storage_probe_sha256': sha(STORAGE),
                   'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'serializer_valid': result['serializer']['serializer_valid'],
                      'matches': result['synthetic_approved_active_match_counts'],
                      'production_written': result['production_written']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
