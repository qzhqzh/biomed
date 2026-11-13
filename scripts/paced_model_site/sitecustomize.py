"""Enable the study-only per-tensor reader in the two local model runners."""

import os
import sys
from pathlib import Path

RUNNERS = {'benchmark_passage_screening.py', 'benchmark_knowledge_candidates.py'}
MODEL = Path('/data/models/Qwen3.5-9B').resolve()

if (os.environ.get('BIOMED_PACED_MODEL_IO') == '1'
        and (Path(sys.argv[0]).name in RUNNERS or os.environ.get('BIOMED_PACED_VERIFY') == '1')):
    try:
        import safetensors

        from scripts.paced_safetensors import PacedSafeOpen

        original = safetensors.safe_open

        def paced_open(path, *args, **kwargs):
            resolved = Path(path).resolve()
            if resolved.parent == MODEL and resolved.suffix == '.safetensors':
                return PacedSafeOpen(original, path, *args, **kwargs)
            return original(path, *args, **kwargs)

        paced_open.biomed_paced = True
        safetensors.safe_open = paced_open
    except Exception:  # noqa: BLE001 - never silently run an unpaced model
        # Python normally continues after a sitecustomize exception.  That
        # would silently start an unpaced large-model load, so fail closed.
        sys.stderr.write('Cannot install study model I/O pacing; stopping.\n')
        os._exit(70)
