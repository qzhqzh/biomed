#!/usr/bin/env python3
"""Fetch pinned checkpoints into the shared model store, reusing verified files."""

import argparse
import fcntl
import hashlib
import json
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL_STORE = Path("/data/models")


def download_file(url, partial, size, rate, *, attempts=5, retry_delay=10):
    """Start a fresh curl for each retry so its resume offset includes new bytes.

    curl's internal retry can roll the output back to its initial offset. Keep
    retries here instead, preserving every successfully received byte on disk.
    """
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            return
        if offset > size:
            raise ValueError(f"Oversized partial download: {partial.name}")
        result = subprocess.run([
            "curl", "--http1.1", "--fail", "--location", "--silent", "--show-error",
            "--connect-timeout", "30", "--max-time", "3600",
            "--speed-limit", "1024", "--speed-time", "120",
            "--continue-at", "-", "--limit-rate", str(rate),
            "--output", str(partial), url,
        ])
        current = partial.stat().st_size if partial.exists() else 0
        if current < offset:
            raise RuntimeError("Download unexpectedly discarded an existing partial file")
        if result.returncode == 0 and current == size:
            return
        if attempt + 1 < attempts:
            print(json.dumps({"event": "retry_wait", "file": partial.name,
                "attempt": attempt + 1, "retained_bytes": current, "curl_code": result.returncode}), flush=True)
            time.sleep(retry_delay * (attempt + 1))
    raise RuntimeError(f"Download paused after {attempts} attempts; partial retained: {partial}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("revision")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--rate", default="28M")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--endpoint", choices=("https://huggingface.co", "https://hf-mirror.com"), default="https://huggingface.co")
    args = parser.parse_args()
    if not args.destination.resolve().is_relative_to(MODEL_STORE.resolve()):
        parser.error("Model weights must be stored under /data/models; check existing models before downloading")
    args.destination.mkdir(parents=True, exist_ok=True)
    # All instances of this downloader share one bandwidth budget, not one per model.
    network_lock = (MODEL_STORE / ".benchmark-download.lock").open("a")
    fcntl.flock(network_lock, fcntl.LOCK_EX)
    lock = (args.destination / "download.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    manifest = args.destination / "download-manifest.json"
    if manifest.exists():
        previous = json.loads(manifest.read_text())
        if previous["repo"] != args.repo or previous["revision"] != args.revision:
            raise ValueError("Checkpoint identity changed; use another directory")
        selected = previous["files"]
    else:
        url = f"https://huggingface.co/api/models/{args.repo}/revision/{args.revision}?blobs=true"
        with urllib.request.urlopen(url, timeout=60) as response:
            info = json.load(response)
        if info["sha"] != args.revision:
            raise ValueError("Resolved checkpoint differs from pinned revision")
        selected = [item for item in info["siblings"] if item["rfilename"].endswith(
            (".safetensors", ".json", ".txt", ".jinja")) or item["rfilename"] in {"README.md", "LICENSE"}]
    # Divide the total ceiling across at most four independent checkpoint files.
    unit = args.rate[-1:].upper()
    ceiling = int(args.rate[:-1]) * {"K": 1024, "M": 1024**2, "G": 1024**3}[unit] if unit in "KMG" else int(args.rate)
    if not 0 < ceiling <= 28 * 1024**2:
        parser.error("Model download budget must be positive and at most 28 MiB/s in total")
    per_file_rate = str(max(1, ceiling // args.workers))
    plan = {"repo": args.repo, "revision": args.revision, "files": selected, "rate": args.rate,
            "workers": args.workers, "download_endpoint": args.endpoint}
    if manifest.exists():
        previous = json.loads(manifest.read_text())
        if any(previous[key] != plan[key] for key in ("repo", "revision", "files")):
            raise ValueError("Checkpoint identity changed; use another directory")
    manifest.write_text(json.dumps(plan, indent=2) + "\n")
    def fetch(item):
        name = item["rfilename"]
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Invalid repository path")
        target = args.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        if not target.exists():
            partial = target.with_name(target.name + ".partial")
            print(json.dumps({"event": "downloading", "repo": args.repo, "file": name, "bytes": item["size"]}), flush=True)
            download_file(f"{args.endpoint}/{args.repo}/resolve/{args.revision}/{name}",
                partial, item["size"], per_file_rate)
            if partial.stat().st_size != item["size"]:
                raise ValueError(f"Wrong file size: {name}")
            partial.rename(target)
        if target.stat().st_size != item["size"]:
            raise ValueError(f"Wrong existing file size: {name}")
        expected = item.get("lfs", {}).get("sha256")
        if expected:
            sha = hashlib.sha256()
            with target.open("rb") as stream:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    sha.update(block)
            if sha.hexdigest() != expected:
                raise ValueError(f"Checkpoint checksum mismatch: {name}")
        print(json.dumps({"event": "verified", "repo": args.repo, "file": name, "seconds": round(time.monotonic() - start, 2)}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(fetch, selected))
    (args.destination / "download-complete.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"event": "completed", "repo": args.repo}), flush=True)


if __name__ == "__main__":
    main()
