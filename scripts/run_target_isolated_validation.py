"""Launch the same target image offline with read-only sources and synthetic data."""

import hashlib
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TARGET = Path("/home/zhuqin/project/zhenyuan/tumor_api/src")
IDENTITY = Path("/tmp/biomed-two-target-identities-v1.json")
PREVIEW = PROJECT / "var/experiments/target-identity-scope-20261112-v1/target-preview-astra-reviewed-v1.json"
DICTIONARY = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
OUTPUT = Path("/tmp/biomed-two-target-runtime-v2.json")
HARNESS = PROJECT / "scripts/probe_target_isolated_runtime.py"
IMAGE = "registry.cn-shanghai.aliyuncs.com/bioit/tumor_api:latest"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(cmd):
    return subprocess.check_output(cmd, text=True, timeout=30).strip()


def run():
    identity = json.loads(IDENTITY.read_text(encoding="utf-8"))
    if identity["preview_sha256"] != sha(PREVIEW) or identity["dictionary_sha256"] != sha(DICTIONARY):
        raise ValueError("Current ID check does not bind to the intended frozen preview")
    running_image = output(["docker", "inspect", "zhuqin-tumor-api", "--format", "{{.Image}}"])
    cached_image = output(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"])
    if running_image != cached_image:
        raise ValueError("Cached image differs from the currently running target")
    mounts = [(TARGET, "/opt/api"), (Path("/home/zhuqin/project/zhenyuan/drf_somatic_biomeddb"), "/opt/drf_somatic_biomeddb"),
              (Path("/home/zhuqin/project/zhenyuan/drf_core"), "/opt/drf_core"),
              (Path("/home/zhuqin/project/zhenyuan/sdk_aliyun_oss"), "/opt/sdk_aliyun_oss"),
              (Path("/home/zhuqin/project/zhenyuan/sdk_event_bus"), "/opt/sdk_event_bus"),
              (HARNESS, "/test/probe.py"), (IDENTITY, "/test/identity.json"), (PREVIEW.resolve(), "/test/preview.json")]
    cmd = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--user", "1000:1000",
           "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=1g", "--tmpfs", "/tmp:size=128m"]
    for source, destination in mounts:
        cmd.extend(("--mount", f"type=bind,src={source},dst={destination},readonly"))
    cmd.extend(("--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONWARNINGS=ignore", "--entrypoint", "python",
                cached_image, "-B", "/test/probe.py"))
    completed = subprocess.run(cmd, text=True, capture_output=True, timeout=150, check=False)
    if completed.returncode:
        # The isolated container receives no production secrets or environment.
        raise RuntimeError(f"Isolated target runtime failed: {completed.stderr[-5000:]}")
    lines = [line.removeprefix("BIOMED_RESULT=") for line in completed.stdout.splitlines() if line.startswith("BIOMED_RESULT=")]
    if len(lines) != 1:
        raise ValueError("Isolated target runtime did not produce exactly one result")
    result = json.loads(lines[0])
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    previous_hashes = dictionary["source"]["source_files"]
    result["source_matches_frozen"] = {name: digest == previous_hashes.get("src/" + name)
                                       for name, digest in result["source_sha256"].items()
                                       if "src/" + name in previous_hashes}
    result.update({"target_running_image_id": running_image, "isolated_image_id": cached_image,
                   "preview_sha256": sha(PREVIEW), "identity_sha256": sha(IDENTITY),
                   "harness_sha256": sha(HARNESS), "network": "none", "container_root": "read-only",
                   "host_mounts": "read-only", "database_connections": "forbidden", "target_written": False,
                   "patient_records_accessed": False, "target_migrations_executed": False})
    return result


if __name__ == "__main__":
    if OUTPUT.exists():
        raise FileExistsError("Keep the first result; choose a new output version")
    result = run()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"cases": [{"pmid": c["pmid"], "serializer_valid": c["serializer"]["valid"],
                                 "matching": c["active_approved_simulation_matches"],
                                 "ungraded_report": c["ungraded_treatment_report"]["status"]}
                                for c in result["results"]], "target_written": result["target_written"],
                      "network": result["network"]}, ensure_ascii=False))
