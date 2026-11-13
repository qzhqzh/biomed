"""Run the synthetic duplicate-drug check in a network-isolated target image."""

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.run_target_isolated_validation import HARNESS, TARGET, output, sha

PROJECT = Path(__file__).resolve().parents[1]
STORAGE = PROJECT/'scripts/probe_target_memory_storage.py'
PROBE = PROJECT/'scripts/probe_target_duplicate_drug_memory.py'
DEVELOPMENT = PROJECT/'var/experiments/target-holdout-20261113-v1/fusion-development-v1.json'
DRUGS = PROJECT/'var/experiments/fusion-independent-20261113-v1/drug-target-identity-observation.json'
OUTPUT = Path('/tmp/biomed-039-target-duplicate-drug-memory.json')
CONTAINER_NAME = 'biomed-target-duplicate-drug-validation-039'


def main():
    if OUTPUT.exists():
        raise FileExistsError('Preserve the first isolated result')
    image = output(['docker', 'inspect', 'zhuqin-tumor-api', '--format', '{{.Image}}'])
    if not image.startswith('sha256:'):
        raise ValueError('Running target did not report an immutable image ID')
    mounts = [(TARGET, '/opt/api'),
              (Path('/home/zhuqin/project/zhenyuan/drf_somatic_biomeddb'), '/opt/drf_somatic_biomeddb'),
              (Path('/home/zhuqin/project/zhenyuan/drf_core'), '/opt/drf_core'),
              (Path('/home/zhuqin/project/zhenyuan/sdk_aliyun_oss'), '/opt/sdk_aliyun_oss'),
              (Path('/home/zhuqin/project/zhenyuan/sdk_event_bus'), '/opt/sdk_event_bus'),
              (HARNESS, '/test/probe_target_isolated_runtime.py'),
              (STORAGE, '/test/probe_target_memory_storage.py'), (PROBE, '/test/duplicate_drug.py'),
              (DEVELOPMENT.resolve(), '/test/development.json'), (DRUGS.resolve(), '/test/drugs.json')]
    cmd = ['docker', 'run', '--name', CONTAINER_NAME, '--rm', '--pull=never', '--network=none', '--read-only',
           '--user', '1000:1000', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128',
           '--memory=1g', '--cpus=0.5', '--tmpfs', '/tmp:size=128m']
    for source, destination in mounts:
        cmd.extend(('--mount', f'type=bind,src={source},dst={destination},readonly'))
    cmd.extend(('--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONWARNINGS=ignore',
                '--entrypoint', 'python', image, '-B', '/test/duplicate_drug.py'))
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=150, check=False)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(['docker', 'rm', '--force', CONTAINER_NAME], text=True, capture_output=True,
                           timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    if completed.returncode:
        raise RuntimeError(f'Isolated duplicate-drug check failed: {completed.stderr[-3000:]}')
    lines = [line.removeprefix('BIOMED_DRUG_DUPLICATE_RESULT=') for line in completed.stdout.splitlines()
             if line.startswith('BIOMED_DRUG_DUPLICATE_RESULT=')]
    if len(lines) != 1:
        raise ValueError('Expected exactly one isolated result')
    result = json.loads(lines[0])
    result.update({'running_image_id': image, 'target_drug_observation_sha256': sha(DRUGS),
                   'development_shape_sha256': sha(DEVELOPMENT), 'probe_sha256': sha(PROBE),
                   'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('one_id_stored',
          'same_evidence_different_drug_id_rejected_as_duplicate',
          'rewritten_description_different_drug_id_accepted',
          'two_alias_ids_in_one_conclusion_accepted_as_two_drugs', 'production_written')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
