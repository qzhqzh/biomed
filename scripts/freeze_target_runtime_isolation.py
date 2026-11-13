"""Freeze the isolated two-candidate runtime study and its small code inputs."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-runtime-isolation-20261112-v1")
INPUTS = {
    "target-identities-readonly.json": Path("/tmp/biomed-two-target-identities-v1.json"),
    "target-treatment-mode-readonly.json": Path("/tmp/biomed-target-treatment-mode-v1.json"),
    "target-runtime-synthetic.json": Path("/tmp/biomed-two-target-runtime-v2.json"),
    "target-memory-storage.json": Path("/tmp/biomed-two-target-memory-storage-v3.json"),
    "study-context-comparison.json": Path("/tmp/biomed-two-target-context-v4.json"),
}
CODE = (
    "scripts/probe_two_target_identities.py", "scripts/probe_target_treatment_mode.py",
    "scripts/probe_target_isolated_runtime.py", "scripts/run_target_isolated_validation.py",
    "scripts/probe_target_memory_storage.py", "scripts/run_target_memory_storage.py",
    "scripts/evaluate_target_condition_context.py", "apps/curation/evidence_scope_context.py",
    "apps/curation/test_evidence_scope_context.py",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name):
    return json.loads(INPUTS[name].read_text(encoding="utf-8"))


def freeze():
    identity, mode = load("target-identities-readonly.json"), load("target-treatment-mode-readonly.json")
    runtime, storage = load("target-runtime-synthetic.json"), load("target-memory-storage.json")
    context = load("study-context-comparison.json")
    if (not identity["transaction_read_only"] or not identity["drugs_all_present"] or not identity["tumors_all_present"]
            or identity["target_written"] or not mode["transaction_read_only"] or mode["target_written"]
            or storage["production_connected"] or storage["production_written"] or runtime["target_written"]
            or storage["identity_sha256"] != sha(INPUTS["target-identities-readonly.json"])
            or runtime["identity_sha256"] != sha(INPUTS["target-identities-readonly.json"])
            or storage["preview_sha256"] != runtime["preview_sha256"]
            or context["preview_sha256"] != runtime["preview_sha256"]
            or context["memory_storage_sha256"] != sha(INPUTS["target-memory-storage.json"])
            or context["isolated_runtime_sha256"] != sha(INPUTS["target-runtime-synthetic.json"])
            or context["method_sha256"] != sha(PROJECT / "apps/curation/evidence_scope_context.py")
            or storage["storage_probe_sha256"] != sha(PROJECT / "scripts/probe_target_memory_storage.py")
            or storage["harness_sha256"] != sha(PROJECT / "scripts/probe_target_isolated_runtime.py")):
        raise ValueError("Isolated study artifacts do not bind to each other")
    if (len(storage["results"]) != 2 or len(runtime["results"]) != 2 or len(context["results"]) != 2
            or not all(row["serializer_valid"] and not row["stored_active"] and row["stored_level"] == "-"
                       and row["consumers"]["ungraded_pending_management_text_search"] for row in storage["results"])):
        raise ValueError("Results do not match the recorded ungraded knowledge test")
    input_bytes = {name: path.read_bytes() for name, path in INPUTS.items()}
    code_bytes = {name: (PROJECT / name).read_bytes() for name in CODE}
    OUTPUT.mkdir(parents=True, exist_ok=False)
    for name, data in input_bytes.items():
        with (OUTPUT / name).open("xb") as stream:
            stream.write(data)
    for name, data in code_bytes.items():
        path = OUTPUT / "code" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    manifest = {"version": "biomed.target-runtime-isolation-manifest.v1",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "preview_sha256": runtime["preview_sha256"], "target_image_id": runtime["target_running_image_id"],
                "inputs": {name: hashlib.sha256(data).hexdigest() for name, data in input_bytes.items()},
                "code": {name: hashlib.sha256(data).hexdigest() for name, data in code_bytes.items()},
                "target_read_only": True, "target_database_written": False, "patient_data_queried": False,
                "isolated_database": "ephemeral SQLite :memory:", "target_migrations_executed": False,
                "target_runtime_mode_at_read": mode["effective_mode"],
                "note": "Stored target evidence IDs in the isolated result are synthetic and do not exist in production."}
    with (OUTPUT / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    return {"artifacts": len(input_bytes), "small_code_files": len(code_bytes), "target_written": False}


if __name__ == "__main__":
    print(json.dumps(freeze(), ensure_ascii=False))
