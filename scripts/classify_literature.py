#!/usr/bin/env python3
"""Bounded, resumable offline inference. Does not acquire papers or train models."""

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.curation.decision import (
    SCOPE_VERSION,
    VERSION,
    checked_input,
    digest,
    system_prompt,
    validate_decision,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def model_fingerprint(folder):
    files = sorted(
        p
        for p in folder.iterdir()
        if p.name.endswith((".safetensors", ".json", ".txt")) and p.name != "README.md"
    )
    if not any(p.suffix == ".safetensors" for p in files):
        raise ValueError("Local safetensors weights required; no downloads")
    result = {}
    for path in files:
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                sha.update(chunk)
        result[path.name] = {"sha256": sha.hexdigest(), "bytes": path.stat().st_size}
    return result


def run(args):
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ[key] = "1"
    os.environ["USE_TF"] = "0"
    os.environ["USE_FLAX"] = "0"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

    torch.set_num_threads(args.threads)
    if not torch.cuda.is_available():
        raise ValueError("CUDA unavailable; refusing an unmeasured CPU fallback")
    torch.cuda.set_per_process_memory_fraction(0.5)
    rows = []
    for path in args.inputs:
        rows.extend(
            checked_input(row) for row in json.loads(Path(path).read_text())["records"]
        )
    rows = rows[: args.limit]
    if not rows or len({row["pmid"] for row in rows}) != len(rows):
        raise ValueError("Empty or duplicate input records")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = {
            "version": SCOPE_VERSION if args.profile == "relevance" else VERSION,
            "model_id": args.model_id,
            "model_files": model_fingerprint(args.model),
            "profile": args.profile,
            "prompt": system_prompt(args.profile),
            "dtype": "float16",
            "attention": "eager",
            "decision_code_sha256": hashlib.sha256(
                (
                    Path(__file__).resolve().parents[1] / "apps/curation/decision.py"
                ).read_bytes()
            ).hexdigest(),
            "enable_thinking": False,
            "do_sample": False,
            "max_input_tokens": args.max_input_tokens,
            "max_new_tokens": args.max_new_tokens,
            "torch": torch.__version__,
            "transformers": transformers.__version__,
        }
        manifest = {
            "config": config,
            "config_sha256": digest(config),
            "input_sha256": digest(rows),
            "records": len(rows),
            "model_path": str(args.model.resolve()),
            "created_at": now(),
            "device": torch.cuda.get_device_name(0),
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            "code_sha256": {
                str(
                    path.relative_to(Path(__file__).resolve().parents[1])
                ): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [
                    Path(__file__).resolve(),
                    Path(__file__).resolve().parents[1] / "apps/curation/decision.py",
                ]
            },
        }
        manifest_path = args.output / "manifest.json"
        if manifest_path.exists():
            original = json.loads(manifest_path.read_text())
            if any(
                original[k] != manifest[k]
                for k in ("config_sha256", "input_sha256", "code_sha256")
            ):
                raise ValueError(
                    "Run inputs/config/code changed; use a new output directory"
                )
            manifest = original
        else:
            atomic_json(args.output / "inputs.json", {"records": rows})
            atomic_json(manifest_path, manifest)
        completed = {}
        for path in sorted(args.output.glob("result-*.json")):
            row = json.loads(path.read_text())
            completed[row["pmid"]] = row
        allowed = {row["pmid"]: row for row in rows}
        for pmid, row in completed.items():
            if (
                pmid not in allowed
                or row["config_sha256"] != manifest["config_sha256"]
                or row["input_sha256"] != allowed[pmid]["input_sha256"]
            ):
                raise ValueError("Existing result has mismatched provenance")

        previous_summary_path = args.output / "summary.json"
        previous_summary = (
            json.loads(previous_summary_path.read_text())
            if previous_summary_path.exists()
            else {}
        )

        def save_summary(state, **extra):
            elapsed = [r["seconds"] for r in completed.values()]
            summary = {
                "state": state,
                "updated_at": now(),
                "total": len(rows),
                "processed": len(completed),
                "statuses": dict(Counter(r["status"] for r in completed.values())),
                "relevance": dict(
                    Counter(
                        r["decision"]["relevance"]
                        for r in completed.values()
                        if r.get("decision")
                    )
                ),
                "inference_seconds": round(sum(elapsed), 3),
                "mean_seconds": round(sum(elapsed) / len(elapsed), 3)
                if elapsed
                else None,
                "peak_allocated_mib": max(
                    round(torch.cuda.max_memory_allocated() / 1024**2, 1),
                    previous_summary.get("peak_allocated_mib", 0),
                ),
                "training": False,
                "network_requests": 0,
                **extra,
            }
            atomic_json(args.output / "summary.json", summary)
            return summary

        if len(completed) == len(rows):
            print(json.dumps(save_summary("completed"), ensure_ascii=False), flush=True)
            return
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
        save_summary("running", load_seconds=load_seconds)
        started = time.monotonic()
        for row in rows:
            if row["pmid"] in completed:
                continue
            if (
                args.output / "STOP"
            ).exists() or time.monotonic() - started > args.max_minutes * 60:
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
                    k: row[k]
                    for k in ("pmid", "version_id", "source_sha256", "input_sha256")
                },
                "config_sha256": manifest["config_sha256"],
                "created_at": now(),
                "input_tokens": count,
                "output_tokens": 0,
                "seconds": 0,
                "status": "input_too_long",
                "raw_output": "",
                "decision": None,
            }
            if count <= args.max_input_tokens:
                start = time.monotonic()
                inputs = inputs.to("cuda")
                with torch.inference_mode():
                    output = model.generate(**inputs, generation_config=generation)
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
                        result["decision"] = validate_decision(
                            result["raw_output"], row["metadata"]
                        )
                        result["status"] = "validated_candidate"
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        result["status"] = "invalid_output"
                        result["error"] = str(exc)
            atomic_json(args.output / f"result-{row['pmid']}.json", result)
            completed[row["pmid"]] = result
            print(
                json.dumps(
                    {
                        "processed": len(completed),
                        "pmid": row["pmid"],
                        "status": result["status"],
                        "seconds": result["seconds"],
                    }
                ),
                flush=True,
            )
            save_summary("running", load_seconds=load_seconds)
        print(
            json.dumps(
                save_summary("completed", load_seconds=load_seconds), ensure_ascii=False
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-id", default="Qwen/Qwen3-1.7B")
    parser.add_argument(
        "--profile", choices=["multi_axis", "relevance"], default="multi_axis"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--max-input-tokens", type=int, default=6144)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    parser.add_argument("--max-minutes", type=int, default=60)
    options = parser.parse_args()
    if (
        not 1 <= options.limit <= 1000
        or not 1 <= options.threads <= 4
        or not 1 <= options.max_minutes <= 120
    ):
        parser.error("limit=1..1000, threads=1..4, max-minutes=1..120")
    if (
        not 256 <= options.max_input_tokens <= 6144
        or not 64 <= options.max_new_tokens <= 1024
    ):
        parser.error("input tokens=256..6144; output tokens=64..1024")
    run(options)
