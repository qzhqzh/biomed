"""Run target serializer persistence in an isolated, ephemeral memory database."""

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.run_target_isolated_validation import (
    DICTIONARY,
    HARNESS,
    IDENTITY,
    IMAGE,
    PREVIEW,
    TARGET,
    output,
    sha,
)

PROJECT = Path(__file__).resolve().parents[1]
STORAGE_PROBE = PROJECT / "scripts/probe_target_memory_storage.py"
OUTPUT = Path("/tmp/biomed-two-target-memory-storage-v3.json")


def run():
    identity = json.loads(IDENTITY.read_text(encoding="utf-8"))
    if identity["preview_sha256"] != sha(PREVIEW) or identity["dictionary_sha256"] != sha(DICTIONARY):
        raise ValueError("Target identity snapshot does not bind to this preview")
    image = output(["docker", "inspect", "zhuqin-tumor-api", "--format", "{{.Image}}"])
    if image != output(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]):
        raise ValueError("Cached image differs from currently running target")
    mounts = [(TARGET, "/opt/api"), (Path("/home/zhuqin/project/zhenyuan/drf_somatic_biomeddb"), "/opt/drf_somatic_biomeddb"),
              (Path("/home/zhuqin/project/zhenyuan/drf_core"), "/opt/drf_core"),
              (Path("/home/zhuqin/project/zhenyuan/sdk_aliyun_oss"), "/opt/sdk_aliyun_oss"),
              (Path("/home/zhuqin/project/zhenyuan/sdk_event_bus"), "/opt/sdk_event_bus"),
              (HARNESS, "/test/probe_target_isolated_runtime.py"), (STORAGE_PROBE, "/test/storage.py"),
              (IDENTITY, "/test/identity.json"), (PREVIEW.resolve(), "/test/preview.json")]
    cmd = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--user", "1000:1000",
           "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=1g", "--tmpfs", "/tmp:size=128m"]
    for source, destination in mounts:
        cmd.extend(("--mount", f"type=bind,src={source},dst={destination},readonly"))
    cmd.extend(("--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONWARNINGS=ignore", "--entrypoint", "python",
                image, "-B", "/test/storage.py"))
    completed = subprocess.run(cmd, text=True, capture_output=True, timeout=150, check=False)
    if completed.returncode:
        raise RuntimeError(f"Isolated in-memory persistence failed: {completed.stderr[-5000:]}")
    lines = [line.removeprefix("BIOMED_STORAGE_RESULT=") for line in completed.stdout.splitlines()
             if line.startswith("BIOMED_STORAGE_RESULT=")]
    if len(lines) != 1:
        raise ValueError("Expected exactly one isolated persistence result")
    result = json.loads(lines[0])
    result.update({"target_running_image_id": image, "preview_sha256": sha(PREVIEW), "identity_sha256": sha(IDENTITY),
                   "harness_sha256": sha(HARNESS), "storage_probe_sha256": sha(STORAGE_PROBE),
                   "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return result


if __name__ == "__main__":
    if OUTPUT.exists():
        raise FileExistsError("Keep the first result; choose a new version")
    result = run()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"cases": len(result["results"]), "serializer_create": result["real_target_serializer_create"],
                      "database": result["database"], "production_written": result["production_written"]}))
