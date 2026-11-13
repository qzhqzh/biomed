"""Freeze only the small code files used in this offline evaluation."""

import argparse
import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-identity-scope-20261112-v1")
FILES = (
    "apps/curation/source_linked_target_repair.py",
    "apps/curation/source_linked_target_mapping.py",
    "apps/curation/source_linked_facts.py",
    "apps/curation/source_disease_projection.py",
    "apps/curation/source_study_context.py",
    "apps/curation/source_context_retrieval.py",
    "apps/curation/knowledge_target_mapping.py",
    "apps/curation/knowledge_target_readiness.py",
    "apps/curation/knowledge_clinical_mapping.py",
    "apps/curation/knowledge_export.py",
    "apps/curation/knowledge_eligibility.py",
    "apps/curation/qualified_disease_names.py",
    "apps/curation/decision.py",
    "apps/curation/astra_reference_integrity.py",
    "apps/curation/astra_assessment_integrity.py",
    "apps/curation/test_source_linked_target_repair.py",
    "scripts/evaluate_entity_scope_trial.py",
)


def freeze(comparison_path, directory):
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    files = {name: (PROJECT / name).read_bytes() for name in FILES}
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if checksums["apps/curation/source_linked_target_repair.py"] != comparison["repair_code_sha256"]:
        raise ValueError("Repair code changed after the frozen comparison")
    directory.mkdir(parents=True, exist_ok=False)
    for name in FILES:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(files[name])
    manifest = {"version": "biomed.entity-scope-code-snapshot.v1", "files": checksums,
                "models_or_project_copied": False, "target_project_modified": False}
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    return {"files": len(checksums), "repair_sha256": comparison["repair_code_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", type=Path, default=ROOT / "comparison-v5.json")
    parser.add_argument("--output", type=Path, default=ROOT / "code-snapshot-v1")
    args = parser.parse_args()
    print(json.dumps(freeze(args.comparison, args.output), ensure_ascii=False))
