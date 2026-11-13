"""Compare frozen old exports with an isolated source-linked target preview."""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from apps.curation.decision import digest
from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_linked_facts import normalize
from apps.curation.source_linked_target_mapping import preview
from scripts.knowledge_observation_report import digest as snapshot_digest

PROJECT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(samples, pilot, dictionary_path, gene_path, ontology_path):
    started = time.perf_counter()
    dataset, lineage = read(pilot / "inputs.json"), read(pilot / "lineage.json")
    manifest = read(pilot / "extract/manifest.json")
    if (digest(dataset) != lineage["inputs_sha256"] or digest(dataset) != manifest["input_sha256"]
            or digest(manifest["config"]) != manifest["config_sha256"]
            or read(pilot / "extract/inputs.json") != dataset):
        raise ValueError("Frozen inference inputs or configuration differ")
    samples_by_id = {row["group_id"]: row for row in samples}
    lineage_by_id = {row["group_id"]: row for row in lineage["records"]}
    skipped_by_id = {row["group_id"]: row for row in lineage.get("skipped", [])}
    rows = dataset["records"]
    if (len(samples_by_id) != len(samples) or len(lineage_by_id) != len(lineage["records"])
            or len(skipped_by_id) != len(lineage.get("skipped", []))
            or set(lineage_by_id) & set(skipped_by_id)
            or len({row["id"] for row in rows}) != len(rows)
            or {row["id"] for row in rows} != set(lineage_by_id)):
        raise ValueError("Inference groups differ from frozen lineage")
    dictionary, standard = read(dictionary_path), load_gene_alias_standard(gene_path)
    nodes, ontology = read(ontology_path), {"sha256": sha(ontology_path)}
    dictionary_sha, results, normalize_counts = sha(dictionary_path), [], Counter()
    for row in rows:
        sample = samples_by_id[row["id"]]
        if (snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["sample_sha256"] != lineage_by_id[row["id"]]["sample_sha256"]
                or sample["source_manifest"]["dictionary_sha256"] != dictionary_sha
                or (sample["source_manifest"].get("gene_standard_sha256") is not None
                    and sample["source_manifest"]["gene_standard_sha256"] != standard["source"]["sha256"])
                or row["input_sha256"] != digest({"id": row["id"], "prompt": row["prompt"]})):
            raise ValueError(f"Frozen sample, dictionary or model input differs: {row['id']}")
        model = read(pilot / "extract" / f"result-{row['id']}.json")
        if (model["id"] != row["id"] or model["input_sha256"] != row["input_sha256"]
                or model["config_sha256"] != manifest["config_sha256"]):
            raise ValueError(f"Model output does not match its frozen input: {row['id']}")
        payload = model.get("payload") if model.get("status") == "classified" else None
        if payload is None:
            normalize_counts["invalid_or_unavailable"] += 1
        else:
            try:
                normalize_counts[normalize(sample, payload)["status"]] += 1
            except (ValueError, TypeError, KeyError):
                normalize_counts["invalid_source_or_shape"] += 1
        result = preview(sample, payload, dictionary=dictionary, gene_standard=standard,
                         disease_nodes=nodes, ontology_source=ontology)
        result["model_result"] = {k: model.get(k) for k in ("id", "status", "input_sha256", "config_sha256", "seconds")}
        result["pmid"] = sample["pmid"]
        result["baseline_gene_standard_sha256"] = sample["source_manifest"].get("gene_standard_sha256")
        results.append(result)
    for group, skipped in skipped_by_id.items():
        sample = samples_by_id[group]
        if (snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["sample_sha256"] != skipped["sample_sha256"]
                or sample["source_manifest"]["dictionary_sha256"] != dictionary_sha
                or (sample["source_manifest"].get("gene_standard_sha256") is not None
                    and sample["source_manifest"]["gene_standard_sha256"] != standard["source"]["sha256"])
                or any(a.get("purpose") == "evidence" for a in sample["record"].get("anchors", []))):
            raise ValueError(f"Skipped source does not match its frozen lineage: {group}")
        result = preview(sample, None, dictionary=dictionary, gene_standard=standard,
                         disease_nodes=nodes, ontology_source=ontology)
        result["reason"] = skipped["reason"]
        result["model_result"] = {"id": group, "status": "skipped_no_evidence", "seconds": 0}
        result["pmid"] = sample["pmid"]
        result["baseline_gene_standard_sha256"] = sample["source_manifest"].get("gene_standard_sha256")
        normalize_counts["skipped_no_evidence"] += 1
        results.append(result)
    counts = Counter(row["status"] for row in results)
    old = sum(bool(row["original_export"].get("offline_ready")) for row in results)
    newly_structured = counts["offline_structure_only"]
    return {
        "version": "biomed.source-linked-target-comparison.v1", "sample_count": len(results),
        "baseline_offline_structure_count": old,
        "new_offline_structure_count_including_preserved_baseline": old + newly_structured,
        "new_offline_structure_candidates": newly_structured,
        "previously_ready_preserved": counts["baseline_preserved"],
        "deferred": counts["deferred"], "normalization_statuses": dict(normalize_counts),
        "independently_verified_usable_facts": None,
        "quality_note": "Exact source links and target field validation are not independent semantic review or proof of safe automatic matching.",
        "duplicates_note": "Records are not counted as independent knowledge until source, semantics and history have been checked.",
        "target_contacted": False, "target_written": False, "training": False,
        "dictionary_sha256": dictionary_sha, "gene_standard_sha256": standard["source"]["sha256"],
        "baseline_gene_standard_unrecorded": [row["group_id"] for row in results if not row["baseline_gene_standard_sha256"]],
        "disease_ontology_sha256": ontology["sha256"], "inference_config_sha256": manifest["config_sha256"],
        "code_sha256": {str(path.relative_to(PROJECT)): sha(path) for path in (
            Path(__file__), PROJECT / "apps/curation/source_linked_facts.py",
            PROJECT / "apps/curation/source_disease_projection.py", PROJECT / "apps/curation/source_linked_target_mapping.py")},
        "adaptation_seconds": round(time.perf_counter() - started, 3),
        "model_summary": read(pilot / "extract/summary.json"), "records": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--genes", type=Path, required=True)
    parser.add_argument("--ontology", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(read(args.samples), args.pilot, args.dictionary, args.genes, args.ontology)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({k: result[k] for k in ("sample_count", "baseline_offline_structure_count",
          "new_offline_structure_count_including_preserved_baseline", "new_offline_structure_candidates",
          "previously_ready_preserved", "deferred", "normalization_statuses", "adaptation_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
