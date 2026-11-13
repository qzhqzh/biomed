"""Append automatically retrieved original context to an isolated probe input."""

import copy
import json
import re
from pathlib import Path

from apps.curation.decision import digest as model_digest
from apps.curation.knowledge_target_mapping import VARIANT
from apps.curation.source_context_retrieval import propose_support
from apps.curation.source_linked_facts import evidence_index, normalize
from scripts.knowledge_observation_report import digest


def prepare(sample_path, model_path, output):
    old = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    if len(old) != 1:
        raise ValueError("Expected one frozen candidate")
    sample = copy.deepcopy(old[0])
    if digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]:
        raise ValueError("Original sample changed")
    old_sha = sample["sample_sha256"]
    model_path = Path(model_path)
    model = json.loads(model_path.read_text(encoding="utf-8"))
    pilot = model_path.parent.parent
    dataset = json.loads((pilot / "inputs.json").read_text(encoding="utf-8"))
    manifest = json.loads((pilot / "extract/manifest.json").read_text(encoding="utf-8"))
    lineage = json.loads((pilot / "lineage.json").read_text(encoding="utf-8"))
    inputs = [r for r in dataset["records"] if r["id"] == sample["group_id"]]
    sources = [r for r in lineage["records"] if r["group_id"] == sample["group_id"]]
    if (len(inputs) != 1 or len(sources) != 1 or sources[0]["sample_sha256"] != old_sha
            or model["id"] != sample["group_id"] or model["status"] != "classified"
            or model["input_sha256"] != inputs[0]["input_sha256"]
            or model_digest(dataset) != manifest["input_sha256"]
            or model_digest(manifest["config"]) != manifest["config_sha256"]
            or model["config_sha256"] != manifest["config_sha256"]
            or json.loads(model["raw_output"]) != model["payload"]):
        raise ValueError("Model output does not match the frozen candidate")
    linked = normalize(sample, model["payload"])
    event, intervention = linked.get("molecular_event"), linked.get("intervention")
    if linked["status"] != "source_linked" or not event or not intervention:
        raise ValueError("Model fields are incomplete; do not guess a retrieval query")
    variants = {m.group(1).upper() for m in VARIANT.finditer(str(event.get("event") or ""))}
    entities = {e["ref"]: e for e in sample["record"]["entities"]}
    drug = entities.get(intervention.get("entity_ref"), {})
    if len(variants) != 1 or drug.get("kind") != "drug":
        raise ValueError("Expected one specific event and located drug")
    retrieval = propose_support(sample, variant=next(iter(variants)), drug=drug["label"])
    if not retrieval["candidates"]:
        raise ValueError("No additional source paragraph matched")
    used_aliases = {(a.get("attributes") or {}).get("alias") for a in sample["record"]["anchors"]}
    numbers = [int(alias[1:]) for alias in used_aliases if isinstance(alias, str) and re.fullmatch(r"P[1-9][0-9]*", alias)]
    number = max(numbers, default=0)
    added = []
    source_by_id = {str(p["id"]): p for p in sample["source_document"]["passages"]}
    for hit in retrieval["candidates"]:
        p = source_by_id[hit["passage_id"]]
        number += 1
        alias = f"P{number}"
        sample["record"]["anchors"].append({"entity_ref": None, "passage_id": p["id"],
            "parser_version": p["parser_version"], "locator": p["locator"], "start": 0, "end": len(p["text"]),
            "quote": p["text"], "purpose": "evidence", "association": "context_only", "roles": [],
            "attributes": {"alias": alias, "input_role": "support", "selection": "exact source-context retrieval"}})
        added.append({"passage_id": p["id"], "locator": p["locator"], "alias": alias})
    sample["record"].setdefault("provenance", {})["positive_context_probe"] = {
        "original_sample_sha256": old_sha, "added_original_passages": added,
        "selection": "automatic exact source-context retrieval; not an independent sample"}
    del sample["sample_sha256"]
    sample["sample_sha256"] = digest(sample)
    evidence_index(sample["record"], sample["source_document"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"version": "target-positive-retrieved-context-v2", "original_sample_sha256": old_sha,
                "new_sample_sha256": sample["sample_sha256"], "additional_passages": added,
                "retrieval": retrieval,
                "target_contacted": False, "target_written": False}
    for name, data in (("samples.json", [sample]), ("manifest.json", manifest)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    return {"output": str(output), "additional_passages": added}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--model-result", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.sample, args.model_result, args.output), ensure_ascii=False))
