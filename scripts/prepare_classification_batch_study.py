"""Freeze a small, stratified, real three-class sample without loading a model."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import checked_input, digest
from scripts.benchmark_knowledge_candidates import load_inputs
from scripts.prepare_knowledge_intake import freeze

BASE = ROOT / "var/experiments/three-class-100-20261023"
MODEL = Path("/data/models/Qwen3.5-4B")
PRODUCTION_DEFINITION = (
    ROOT / "var/experiments/three-class-20261022/task-definition-v1.json"
)
DEFAULT_OUTPUT = ROOT / "var/experiments/classification-batching-20261114-v2"
COUNTS = {"solid": 4, "hematologic": 3, "non_neoplastic": 3}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(
    output, *, base=BASE, model=MODEL, production_definition=PRODUCTION_DEFINITION
):
    output, base, model = Path(output), Path(base), Path(model)
    sources = json.loads((base / "inputs.json").read_text())
    reference = json.loads((base / "astra-reference-v1.json").read_text())
    definition = json.loads((base / "task-definition-v1.json").read_text())
    manifest = json.loads((model / "download-manifest.json").read_text())
    production = json.loads(Path(production_definition).read_text())
    source_rows = [checked_input(row) for row in sources["records"]]
    source_by_id = {str(row["pmid"]): row for row in source_rows}
    if len(source_by_id) != len(source_rows):
        raise ValueError("Duplicate PMID in source sample")
    refs = reference["records"]
    reference_by_id = {str(row["pmid"]): row for row in refs}
    if len(reference_by_id) != len(refs):
        raise ValueError("Duplicate PMID in reference")
    if reference.get("task_version") != definition["task_version"]:
        raise ValueError("Reference task version differs")
    if reference.get("definition_sha256") != file_hash(
        base / "task-definition-v1.json"
    ):
        raise ValueError("Reference definition hash differs")
    if any(production.get(k) != definition.get(k) for k in ("task_version", "prompt")):
        raise ValueError(
            "Frozen reference and production classification prompts differ"
        )
    chosen = {}
    for category, count in COUNTS.items():
        candidates = sorted(
            (
                row
                for row in refs
                if row.get("category") == category
                and row.get("reference_state") == "determinate"
            ),
            key=lambda row: int(row["pmid"]),
        )
        if len(candidates) < count:
            raise ValueError(f"Insufficient determinate references for {category}")
        chosen[category] = candidates[:count]
    # Each 5-paper control has examples of all three categories.
    groups = [
        chosen["solid"][:2] + chosen["hematologic"][:2] + chosen["non_neoplastic"][:1],
        chosen["solid"][2:] + chosen["hematologic"][2:] + chosen["non_neoplastic"][1:],
    ]
    records, references = [], []
    for ref in groups[0] + groups[1]:
        pmid = str(ref["pmid"])
        src = source_by_id.get(pmid)
        if src is None or any(
            ref.get(k) != src.get(k)
            for k in ("version_id", "source_sha256", "input_sha256")
        ):
            raise ValueError(f"Reference source identity differs for {pmid}")
        row = {
            "id": pmid,
            "pmid": pmid,
            "version_id": src["version_id"],
            "source_sha256": src["source_sha256"],
            "metadata_input_sha256": src["input_sha256"],
            "prompt": json.dumps(src["metadata"], ensure_ascii=False),
        }
        row["input_sha256"] = digest(row)
        records.append(row)
        references.append(
            {
                "id": pmid,
                "category": ref["category"],
                "reference_state": ref["reference_state"],
            }
        )
    if len({row["id"] for row in records}) != 10:
        raise ValueError("Sample must contain ten unique articles")
    task = {
        "version": definition["task_version"],
        "system_prompt": definition["prompt"],
    }
    all_inputs = {"records": records}
    spec = {
        "version": "classification-batching-v1",
        "selection": "stratified determinate references, numeric PMID ascending within each category",
        "counts": COUNTS,
        "sample_sha256": digest(all_inputs),
        "reference_sha256": digest({"records": references}),
        "definition_sha256": digest(task),
        "source_inputs_sha256": file_hash(base / "inputs.json"),
        "source_reference_sha256": file_hash(base / "astra-reference-v1.json"),
        "source_definition_sha256": file_hash(base / "task-definition-v1.json"),
        "production_definition_sha256": file_hash(production_definition),
        "model_path": str(model),
        "model_id": manifest["repo"],
        "revision": manifest["revision"],
        "reference_kind": "Astra model reference, not human gold",
        "methods": [
            "one load for ten sequential articles",
            "two loads for five sequential articles each",
        ],
        "measurement_order": ["merged", "split-1", "split-2"],
        "gpu_uuid": "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2",
        "max_input_tokens": 6144,
        "max_new_tokens": 128,
    }
    freeze(output / "inputs-all.json", all_inputs)
    freeze(output / "inputs-split-1.json", {"records": records[:5]})
    freeze(output / "inputs-split-2.json", {"records": records[5:]})
    freeze(output / "definition.json", task)
    freeze(output / "reference.json", {"records": references})
    freeze(output / "sample.json", spec)
    for name in ("inputs-all.json", "inputs-split-1.json", "inputs-split-2.json"):
        load_inputs(output / name)
    return {
        "sample_sha256": spec["sample_sha256"],
        "ids": [r["id"] for r in records],
        "counts": COUNTS,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False))
