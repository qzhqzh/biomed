"""Download one pinned Bonsai GGUF, retaining partials and verifying SHA256."""

import fcntl
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = "prism-ml/Ternary-Bonsai-2-27B-gguf"
REVISION = "b072e1d3b35a0a630cece372c2127528e0994386"
WEIGHT = "Ternary-Bonsai-2-27B-PQ2_0.gguf"
DEST = Path("/data/models/Ternary-Bonsai-2-27B")


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    with Path("/data/models/.benchmark-download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        url = f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true"
        with urllib.request.urlopen(url, timeout=60) as response:
            info = json.load(response)
        if info["sha"] != REVISION:
            raise ValueError("Unexpected revision")
        files = [f for f in info["siblings"] if f["rfilename"] in
                 {WEIGHT, "README.md", "LICENSE", "NOTICE.txt", "KNOWN_ISSUES.md"}]
        manifest = {"repo": REPO, "revision": REVISION, "files": files,
                    "rate": None, "rate_policy": "User requested unlimited downloads 2026-10-30"}
        path = DEST / "download-manifest.json"
        if path.exists():
            previous = json.loads(path.read_text())
            if any(previous[k] != manifest[k] for k in ("repo", "revision", "files")):
                raise ValueError("Existing download has different identity")
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        for item in files:
            target = DEST / item["rfilename"]
            partial = target.with_name(target.name + ".partial")
            source = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{item['rfilename']}"
            if not target.exists():
                if item["rfilename"] == WEIGHT:
                    # aria2 preserves curl's existing contiguous prefix, then records
                    # verified completion ranges in its own .aria2 resume sidecar.
                    print(json.dumps({"event": "parallel_download", "file": target.name,
                                      "connections": 4, "rate": None}), flush=True)
                    subprocess.run([
                        "aria2c", "--continue=true", "--max-connection-per-server=4", "--split=4",
                        "--min-split-size=16M", "--file-allocation=none", "--auto-file-renaming=false",
                        "--allow-overwrite=false", "--max-tries=5", "--retry-wait=10", "--timeout=120",
                        "--console-log-level=warn", "--summary-interval=30", "--enable-color=false",
                        "--dir", str(DEST), "--out", partial.name, source,
                    ], check=True)
                for attempt in range(5):
                    offset = partial.stat().st_size if partial.exists() else 0
                    if offset > item["size"]:
                        raise ValueError("Oversized partial")
                    if offset == item["size"]:
                        break
                    print(json.dumps({"event": "downloading", "file": target.name,
                                      "offset": offset, "bytes": item["size"], "rate": None}), flush=True)
                    result = subprocess.run([
                        "curl", "--http1.1", "--fail", "--location", "--silent", "--show-error",
                        "--connect-timeout", "30", "--max-time", "3600",
                        "--speed-limit", "1024", "--speed-time", "120", "--continue-at", "-",
                        "--output", str(partial), source,
                    ])
                    current = partial.stat().st_size if partial.exists() else 0
                    if current < offset:
                        raise RuntimeError("Download discarded partial bytes")
                    if result.returncode == 0 and current == item["size"]:
                        break
                    if attempt < 4:
                        time.sleep(10 * (attempt + 1))
                if not partial.exists() or partial.stat().st_size != item["size"]:
                    raise RuntimeError("Incomplete download; partial retained")
            candidate = target if target.exists() else partial
            if candidate.stat().st_size != item["size"]:
                raise ValueError("Wrong file size")
            sha = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
                    sha.update(chunk)
            expected = (item.get("lfs") or {}).get("sha256")
            if expected and sha.hexdigest() != expected:
                raise ValueError("SHA256 mismatch; file retained for diagnosis")
            if candidate == partial:
                partial.rename(target)
            print(json.dumps({"event": "verified", "file": target.name,
                              "bytes": item["size"], "sha256": sha.hexdigest()}), flush=True)
        print(json.dumps({"event": "complete", "directory": str(DEST)}), flush=True)


if __name__ == "__main__":
    main()
