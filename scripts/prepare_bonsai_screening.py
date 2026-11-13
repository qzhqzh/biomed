"""Freeze unchanged classification and passage prompts for a Bonsai-only run."""
# ruff: noqa: E402

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.three_class import load_inputs as load_classification_inputs
from apps.curation.three_class_evaluation import validate_reference as validate_classification_reference
from scripts.benchmark_passage_screening import INPUT_FIELDS, load_inputs as load_passage_inputs
from scripts.evaluate_passage_screening import validate_reference as validate_passage_reference

ROOT = PROJECT / "var/experiments/bonsai-stages-20261101"
CLASS_BASE = PROJECT / "var/experiments/three-class-100-20261023"
CLASS_RUNS = PROJECT / "var/experiments/model-comparison-100-20261023"
PASSAGE_BASE = PROJECT / "var/experiments/evidence-passages-20261023"
VERSION = "bonsai-screening-pair-v1"
BASELINES = ("qwen35-4b", "qwen35-9b")


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Frozen artifact changed: {path}")
    else:
        with path.open("xb") as stream:
            stream.write(data)


def freeze(path, data):
    freeze_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def document(stage, row):
    # These are the original runners' inline prompt construction expressions.
    # Preserve insertion order, default separators/spaces and Unicode behavior.
    if stage == "classification":
        return json.dumps(row["metadata"], ensure_ascii=False)
    if stage == "passages":
        return json.dumps({key: row[key] for key in INPUT_FIELDS}, ensure_ascii=False)
    raise ValueError("Unknown stage")


def mapped_inputs(stage, rows):
    output = []
    for source in rows:
        identity = source["pmid"] if stage == "classification" else source["id"]
        row = {"id": identity, "original_id": identity, "pmid": source["pmid"],
               "source_input_sha256": source["input_sha256"], "prompt": document(stage, source)}
        row["input_sha256"] = digest(row)
        output.append(row)
    return {"records": output}


def checked_passage_reference(base, rows, definition_path, reference_path):
    reference = read(reference_path)
    refs = validate_passage_reference(rows, reference, sha(definition_path))
    if reference.get("parent_reference_sha256"):
        parent_path = base / reference["parent_reference_artifact"]
        review_path = base / reference["review_artifact"]
        if sha(parent_path) != reference["parent_reference_sha256"] or sha(review_path) != reference["review_sha256"]:
            raise ValueError("Passage reference revision provenance changed")
        parent = validate_passage_reference(rows, read(parent_path), sha(definition_path))
        review = read(review_path)
        if review["annotator_model"] != "gpt-6-astra" or review["definition_sha256"] != sha(definition_path):
            raise ValueError("Passage reference review definition differs")
        changes = {r["id"]: r for r in review["records"] if r["decision"] == "revise"}
        if {key for key in refs if refs[key] != parent[key]} != set(changes):
            raise ValueError("Unexpected passage reference changes")
        for key, change in changes.items():
            if (parent[key]["relevant"] is not change["previous_relevant"]
                    or refs[key]["relevant"] is not change["recommended_relevant"]
                    or refs[key]["evidence"] != change["evidence"]
                    or refs[key]["reason"] != change["reason"]):
                raise ValueError("Passage reference revision does not match recorded review")
    return refs


def prepare(stage, root=ROOT):
    root = Path(root) / stage
    if stage == "classification":
        base, runs, count, output_limit = CLASS_BASE, CLASS_RUNS, 100, 128
        source_definition = base / "task-definition-v1.json"
        reference_path = base / "astra-reference-v1.json"
        rows = load_classification_inputs([base / "inputs.json"])
        definition = read(source_definition)
        refs = validate_classification_reference(read(reference_path), rows, definition, sha(source_definition))
        if len(rows) != count or sum(r["category"] is not None for r in refs.values()) != 95:
            raise ValueError("Expected frozen 100 papers, 95 determinate references")
        task_version = definition["task_version"]
        extras = []
        baseline_runner = PROJECT / "scripts/benchmark_three_class.py"
    elif stage == "passages":
        base, runs, count, output_limit = PASSAGE_BASE, PASSAGE_BASE, 282, 64
        source_definition = base / "definition.json"
        reference_path = base / "astra-reference-v2.json"
        rows = load_passage_inputs(base / "inputs.json", count)["records"]
        definition = read(source_definition)
        refs = checked_passage_reference(base, rows, source_definition, reference_path)
        if sum(r["relevant"] is True for r in refs.values()) != 122 or sum(r["relevant"] is False for r in refs.values()) != 160:
            raise ValueError("Expected frozen v2 reference: 122 positive, 160 negative")
        task_version = definition["version"]
        extras = [base / "astra-reference-v1.json", base / "reference-review.json"]
        baseline_runner = PROJECT / "scripts/benchmark_passage_screening.py"
    else:
        raise ValueError("Unknown stage")
    for name, path in (("source-inputs.json", base / "inputs.json"),
                       ("source-definition.json", source_definition), ("reference.json", reference_path)):
        freeze_bytes(root / name, path.read_bytes())
    for path in extras:
        freeze_bytes(root / path.name, path.read_bytes())
    freeze(root / "inputs.json", mapped_inputs(stage, rows))
    freeze(root / "definition.json", {"version": task_version, "system_prompt": definition["prompt"]})
    baseline_paths = {model: runs / model for model in BASELINES}
    sources = [base / "inputs.json", source_definition, reference_path, baseline_runner, *extras]
    for folder in baseline_paths.values():
        sources += [folder / "manifest.json", folder / "summary.json", folder / "inputs.json"]
        sources += sorted(folder.glob("result-*.json"))
    plan = {"version": VERSION, "stage": stage, "sample_count": count,
            "source_base": str(base), "baselines": {k: str(v) for k, v in baseline_paths.items()},
            "source_reference": str(reference_path), "frozen_reference_sha256": sha(root / "reference.json"),
            "generation": {"batch_size": 1, "enable_thinking": False, "do_sample": False,
                           "max_input_tokens": 6144, "max_new_tokens": output_limit,
                           "input_truncation": False},
            "prompt_construction": "Exact original inline json.dumps expression; Unicode preserved, default separators, original key order",
            "new_baseline_model_calls": 0, "new_bonsai_scored_calls": count,
            "reference_policy": "Unchanged pre-existing Astra reference; no new labels or adjudication using Bonsai outputs",
            "sources": [{"path": str(path), "sha256": sha(path)} for path in sources]}
    freeze(root / "plan.json", plan)
    print(json.dumps({"stage": stage, "records": count, "output": str(root), "max_new_tokens": output_limit}))
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--stage", choices=("classification", "passages", "all"), default="all")
    args = parser.parse_args()
    for stage in ("classification", "passages") if args.stage == "all" else (args.stage,):
        prepare(stage, args.root)
