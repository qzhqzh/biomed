"""Compare frozen old exports with the source-linked repair on the same records."""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_linked_target_mapping import preview as previous_preview
from apps.curation.source_linked_target_repair import repair
from scripts.evaluate_entity_scope_trial import core_key, load_payload
from scripts.freeze_effective_knowledge_sample import queue_rows
from scripts.knowledge_observation_report import DEFAULT_ROOT, digest, verified_batch

PROJECT = Path(__file__).resolve().parents[1]
DICTIONARY = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
GENES = PROJECT / "data/knowledge/genes.jsonl"
ONTOLOGY = Path("/appdata/02_resource/apps/biomed/resources/cancer-20261025/nodes.json")
YIELD = PROJECT / "var/experiments/target-yield-20261106"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def known_ready_keys(root):
    """Bounded batch ledger and frozen research exports; never scan the main DB."""
    keys = set()
    historical = read(YIELD / "final/tumor-api-export.json")
    for row in historical["records"]:
        if row.get("offline_ready"):
            keys.add(core_key(row))
    batch_groups = {}
    for row in queue_rows(root):
        if row.get("status") == "offline_ready":
            batch_groups.setdefault(row["batch_id"], set()).add(row["group_id"])
    for batch, groups in batch_groups.items():
        _, exports, _, _ = verified_batch(root, {"id": batch})
        for row in exports:
            if row["group_id"] in groups and row.get("offline_ready"):
                keys.add(core_key(row))
    keys.discard(None)
    return keys


def evaluate(root, pilot, output):
    started = time.perf_counter()
    root, pilot, output = Path(root), Path(pilot), Path(output)
    if output.exists():
        raise FileExistsError("The frozen comparison cannot be overwritten")
    samples, selection, manifest = read(root / "samples.json"), read(root / "selection.json"), read(root / "manifest.json")
    reference_validation = read(root / "astra-source-reference-validation.json")
    summary = read(pilot / "extract/summary.json")
    if (digest(samples) != manifest["samples_sha256"] or len(selection) != 20 or len(samples) != 58
            or len({str(row["pmid"]) for row in samples}) != 20 or summary.get("state") != "completed"
            or summary.get("processed") != len(samples)
            or reference_validation.get("source_sha256") != manifest["source_sha256"]
            or reference_validation.get("reference_sha256") != sha(root / "astra-source-reference.json")):
        raise ValueError("Sample, source reference or completed model run differs")
    dictionary, standard, nodes = read(DICTIONARY), load_gene_alias_standard(GENES), read(ONTOLOGY)
    dictionary_sha = sha(DICTIONARY)
    options = {"dictionary": dictionary, "gene_standard": standard, "disease_nodes": nodes,
               "ontology_source": {"sha256": sha(ONTOLOGY)}}
    results = []
    for sample in samples:
        if (digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["source_manifest"].get("dictionary_sha256") != dictionary_sha
                or (sample["source_manifest"].get("gene_standard_sha256") is not None
                    and sample["source_manifest"]["gene_standard_sha256"] != standard["source"]["sha256"])):
            raise ValueError(f"Frozen sample or dictionary differs for {sample['group_id']}")
        payload, model = load_payload(sample, pilot)
        old = previous_preview(sample, payload, **options)
        new = repair(sample, payload, **options)
        results.append({"pmid": sample["pmid"], "group_id": sample["group_id"], "sampling_group": sample["sampling_group"],
                        "sample_sha256": sample["sample_sha256"],
                        "original_export_ready": bool(sample["original_export"].get("offline_ready")),
                        "old_preview": old, "new_preview": new, "model": model})
    known = known_ready_keys(DEFAULT_ROOT)
    repaired = [row for row in results if row["new_preview"]["status"] == "offline_structure_only"]
    keys = [core_key(row["new_preview"]["derived_export"]) for row in repaired]
    unique = {key for key in keys if key is not None}
    report = {"version": "target-holdout-comparison-v1", "article_count": 20, "sample_count": len(results),
              "baseline_offline_count": sum(row["original_export_ready"] for row in results),
              "previous_preview_offline_count": sum(row["old_preview"]["status"] in {"baseline_preserved", "offline_structure_only"}
                                                    for row in results),
              "repair_offline_count": sum(row["new_preview"]["status"] in {"baseline_preserved", "offline_structure_only"}
                                           for row in results),
              "new_offline_candidates": len(repaired), "distinct_new_core_keys": len(unique),
              "not_in_known_historical_ready_keys": sum(key not in known for key in unique),
              "history_ready_key_count": len(known), "model_statuses": dict(Counter(row["model"]["status"] for row in results)),
              "reasons": dict(Counter(row["new_preview"]["reason"] for row in results)),
              "quality_reference_status": "awaiting_independent_astra_candidate_review",
              "quality_note": "Automatic target shape and an exact key do not establish semantic correctness or full-library novelty.",
              "scope_note": "Up to three old candidates per article, not an exhaustive article-level extraction or representative sample.",
              "source_only_sha256": manifest["source_sha256"], "astra_reference_sha256": reference_validation["reference_sha256"],
              "dictionary_sha256": dictionary_sha, "gene_standard_sha256": standard["source"]["sha256"],
              "repair_code_sha256": sha(PROJECT / "apps/curation/source_linked_target_repair.py"),
              "adaptation_seconds": round(time.perf_counter() - started, 3),
              "model_summary": {k: summary.get(k) for k in ("expected", "processed", "statuses", "total_inference_seconds",
                                                            "gpu_process_peak_mib", "sessions")},
              "automatic_clinical_matching": False, "target_contacted": False, "target_written": False,
              "records": results}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return {k: report[k] for k in ("article_count", "sample_count", "baseline_offline_count", "repair_offline_count",
                                   "new_offline_candidates", "distinct_new_core_keys", "reasons", "adaptation_seconds")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pilot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.root, args.pilot, args.output), ensure_ascii=False))
