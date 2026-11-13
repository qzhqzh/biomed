#!/usr/bin/env python3
"""Run a bounded three-class experiment using local weights and immutable inputs."""

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.three_class import (
    CATEGORIES,
    VERSION,
    load_completed,
    load_inputs,
    summarize,
    validate_output,
)
from scripts.classify_literature import atomic_json, model_fingerprint, now


def model_stats(folder):
    return {
        path.name: {"bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in sorted(folder.iterdir())
        if path.name.endswith((".safetensors", ".json", ".txt"))
        and path.name != "README.md"
    }


def run(args):
    rows = load_inputs(args.inputs)
    if len(rows) > args.limit:
        raise ValueError("Input exceeds limit; explicitly freeze a smaller input file")
    definition = json.loads(args.definition.read_text())
    if (
        not isinstance(definition.get("prompt"), str)
        or not definition["prompt"].strip()
    ):
        raise ValueError("Frozen definition must contain a nonempty prompt")
    if definition.get("categories") != list(CATEGORIES):
        raise ValueError(
            "Frozen definition must use exactly the three business categories"
        )
    config = {
        "version": VERSION,
        "definition": definition,
        "prompt": definition["prompt"],
        "model_id": args.model_id,
        "model_path": str(args.model.resolve()),
        "model_file_stats": model_stats(args.model),
        "dtype": "float16",
        "attention": "eager",
        "enable_thinking": False,
        "do_sample": False,
        "threads": args.threads,
        "max_input_tokens": args.max_input_tokens,
        "max_new_tokens": args.max_new_tokens,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            **{
                name: importlib.metadata.version(name)
                for name in ("torch", "transformers")
            },
        },
        "code_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                ROOT / "apps/curation/three_class.py",
                ROOT / "apps/curation/decision.py",
                ROOT / "scripts/classify_literature.py",
            )
        },
    }
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = args.output / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["config_sha256"] != digest(config) or manifest[
                "input_sha256"
            ] != digest(rows):
                raise ValueError(
                    "Run inputs/config/code changed; use a new output directory"
                )
        else:
            manifest = {
                "config": config,
                "config_sha256": digest(config),
                "input_sha256": digest(rows),
                "records": len(rows),
                "model_files": model_fingerprint(args.model),
                "created_at": now(),
            }
            atomic_json(args.output / "inputs.json", {"records": rows})
            atomic_json(manifest_path, manifest)
        completed = load_completed(args.output, rows, manifest["config_sha256"])
        summary_path = args.output / "summary.json"
        previous = json.loads(summary_path.read_text()) if summary_path.exists() else {}

        def save_summary(state, **extra):
            result = {
                **previous,
                **summarize(rows, completed),
                "state": state,
                "updated_at": now(),
                **extra,
            }
            atomic_json(summary_path, result)
            previous.update(result)
            return result

        # A complete checkpoint is reusable without importing torch or initializing CUDA.
        if len(completed) == len(rows):
            print(json.dumps(save_summary("completed", reused=True)), flush=True)
            return
        if (args.output / "STOP").exists():
            print(json.dumps(save_summary("paused")), flush=True)
            return
        os.environ.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            USE_TF="0",
            USE_FLAX="0",
            TOKENIZERS_PARALLELISM="false",
        )
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

        torch.set_num_threads(args.threads)
        if not torch.cuda.is_available():
            raise ValueError("CUDA unavailable; refusing an unmeasured CPU fallback")
        torch.cuda.set_per_process_memory_fraction(0.5)
        save_summary("loading_model")
        start = time.monotonic()
        tokenizer = AutoTokenizer.from_pretrained(
            args.model, local_files_only=True, trust_remote_code=False
        )
        model = (
            AutoModelForCausalLM.from_pretrained(
                args.model,
                local_files_only=True,
                trust_remote_code=False,
                torch_dtype=torch.float16,
                attn_implementation="eager",
            )
            .to("cuda")
            .eval()
        )
        load_seconds = round(time.monotonic() - start, 3)
        generation = GenerationConfig(
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
        print(
            json.dumps(
                {"event": "loaded", "seconds": load_seconds, "records": len(rows)}
            ),
            flush=True,
        )
        started = time.monotonic()
        for row in rows:
            if row["pmid"] in completed:
                continue
            if (
                args.output / "STOP"
            ).exists() or time.monotonic() - started >= args.max_minutes * 60:
                print(
                    json.dumps(save_summary("paused", load_seconds=load_seconds)),
                    flush=True,
                )
                return
            text = tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": config["prompt"]},
                    {
                        "role": "user",
                        "content": json.dumps(row["metadata"], ensure_ascii=False),
                    },
                ],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            inputs = tokenizer(text, return_tensors="pt")
            count = inputs.input_ids.shape[1]
            result = {
                **{
                    key: row[key]
                    for key in ("pmid", "version_id", "source_sha256", "input_sha256")
                },
                "config_sha256": manifest["config_sha256"],
                "created_at": now(),
                "input_tokens": count,
                "output_tokens": 0,
                "seconds": 0,
                "status": "input_too_long",
                "raw_output": "",
                "category": None,
            }
            if count <= args.max_input_tokens:
                torch.cuda.synchronize()
                start = time.monotonic()
                with torch.inference_mode():
                    output = model.generate(
                        **inputs.to("cuda"), generation_config=generation
                    )
                torch.cuda.synchronize()
                tokens = output[0, count:].tolist()
                result.update(
                    seconds=round(time.monotonic() - start, 3),
                    output_tokens=len(tokens),
                    raw_output=tokenizer.decode(tokens, skip_special_tokens=True),
                )
                if not tokens or tokens[-1] != tokenizer.eos_token_id:
                    result["status"] = "output_truncated"
                else:
                    try:
                        result["category"] = validate_output(result["raw_output"])
                        result["status"] = (
                            "classified"
                            if result["category"] is not None
                            else "abstained"
                        )
                    except (ValueError, TypeError) as exc:
                        result.update(status="invalid_output", error=str(exc))
            atomic_json(args.output / f"result-{row['pmid']}.json", result)
            completed[row["pmid"]] = result
            save_summary(
                "running",
                load_seconds=load_seconds,
                device=torch.cuda.get_device_name(0),
                peak_allocated_mib=max(
                    previous.get("peak_allocated_mib", 0),
                    round(torch.cuda.max_memory_allocated() / 1024**2, 1),
                ),
            )
            print(
                json.dumps(
                    {
                        "processed": len(completed),
                        "pmid": row["pmid"],
                        "status": result["status"],
                        "category": result["category"],
                        "seconds": result["seconds"],
                    }
                ),
                flush=True,
            )
        print(json.dumps(save_summary("completed", reused=False)), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--definition", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--model-id", default="Qwen/Qwen3-1.7B")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--max-input-tokens", type=int, default=6144)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--max-minutes", type=int, default=15)
    args = parser.parse_args()
    if not (
        1 <= args.limit <= 1000
        and 1 <= args.threads <= 4
        and 1 <= args.max_minutes <= 120
        and 256 <= args.max_input_tokens <= 6144
        and 16 <= args.max_new_tokens <= 1024
    ):
        parser.error("Invalid execution budget")
    run(args)
