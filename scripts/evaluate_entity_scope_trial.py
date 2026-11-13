"""Evaluate frozen model outputs and historical target controls offline."""

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from apps.curation.decision import digest as model_digest
from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.knowledge_export import digest as export_digest
from apps.curation.source_linked_target_mapping import preview as old_preview
from apps.curation.source_linked_target_repair import repair
from scripts.freeze_effective_knowledge_sample import queue_rows
from scripts.knowledge_observation_report import DEFAULT_ROOT, verified_batch
from scripts.knowledge_observation_report import digest as snapshot_digest

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-identity-scope-20261112-v1")
PRIOR = PROJECT / "var/experiments/knowledge-target-optimization-20261112-v1"
YIELD = PROJECT / "var/experiments/target-yield-20261106"
DICTIONARY = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
GENES = PROJECT / "data/knowledge/genes.jsonl"
ONTOLOGY = Path("/appdata/02_resource/apps/biomed/resources/cancer-20261025/nodes.json")
MODELS = {
    "42719457": PRIOR / "positive-probe-v1/retrieved-context-v2/bonsai-v1",
    "42756523": PRIOR / "bonsai-source-bindings-dev-v1",
    "42840361": PRIOR / "context-probe-v1/bonsai-v1",
    "42864811": PRIOR / "context-probe-v1/bonsai-v1",
    "42817873": PRIOR / "context-probe-v1/bonsai-v1",
    "42784734": ROOT / "bonsai-esr1-v1",
}
CONTROL_GROUPS = ("Y-42872089-0000", "Y-42872145-0000")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_payload(sample, pilot):
    inputs, lineage = read(pilot / "inputs.json"), read(pilot / "lineage.json")
    manifest = read(pilot / "extract/manifest.json")
    rows = [r for r in inputs["records"] if r["id"] == sample["group_id"]]
    origins = [r for r in lineage["records"] if r["group_id"] == sample["group_id"]]
    if (len(rows) != 1 or len(origins) != 1 or origins[0]["sample_sha256"] != sample["sample_sha256"]
            or model_digest(inputs) != manifest["input_sha256"]
            or model_digest(manifest["config"]) != manifest["config_sha256"]
            or rows[0]["input_sha256"] != model_digest({"id": rows[0]["id"], "prompt": rows[0]["prompt"]})):
        raise ValueError("Frozen model inputs or source lineage differ")
    path = pilot / "extract" / f"result-{sample['group_id']}.json"
    result = read(path)
    if (result["id"] != sample["group_id"] or result["input_sha256"] != rows[0]["input_sha256"]
            or result["config_sha256"] != manifest["config_sha256"]):
        raise ValueError("Frozen model output differs")
    if result["status"] == "classified" and json.loads(result["raw_output"]) != result["payload"]:
        raise ValueError("Raw model output and payload disagree")
    return (result.get("payload") if result["status"] == "classified" else None,
            {"status": result["status"], "seconds": result.get("seconds"), "result_sha256": sha(path),
             "input_sha256": result["input_sha256"], "config_sha256": result["config_sha256"]})


def core_key(exported):
    """A conservative duplicate hint, not a semantic equivalence judgment."""
    payload = exported.get("target_payload") or {}
    conclusions = payload.get("conclusion_inputs") or []
    if len(conclusions) != 1:
        return None
    conclusion = conclusions[0]
    triggers, scopes, drugs = conclusion.get("trigger_inputs") or [], conclusion.get("disease_scope_inputs") or [], conclusion.get("drug_ids") or []
    if len(triggers) != 1 or len(scopes) != 1 or any(not isinstance(d, str) for d in drugs):
        return None
    trigger, scope = triggers[0], scopes[0]
    return (payload.get("evidence_category"), trigger.get("event_type"), trigger.get("gene_symbol"),
            str(trigger.get("target_key") or "").casefold().removeprefix("p."),
            scope.get("tumor_id"), scope.get("subtype_id"), tuple(sorted(drugs)))


def evaluate():
    started = time.perf_counter()
    sample_manifest, samples, source_only = read(ROOT / "manifest.json"), read(ROOT / "old-candidates.json"), read(ROOT / "source-only.json")
    if (snapshot_digest(samples) != sample_manifest["candidates_sha256"]
            or snapshot_digest(source_only) != sample_manifest["source_only_sha256"]):
        raise ValueError("Frozen inputs differ from sample manifest")
    dictionary, standard, nodes = read(DICTIONARY), load_gene_alias_standard(GENES), read(ONTOLOGY)
    dictionary_sha = sha(DICTIONARY)
    options = {"dictionary": dictionary, "gene_standard": standard, "disease_nodes": nodes,
               "ontology_source": {"sha256": sha(ONTOLOGY)}}
    results = []
    for sample in samples:
        if (snapshot_digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["source_manifest"]["dictionary_sha256"] != dictionary_sha
                or (sample["source_manifest"].get("gene_standard_sha256") is not None
                    and sample["source_manifest"]["gene_standard_sha256"] != standard["source"]["sha256"])):
            raise ValueError(f"Frozen source or dictionary differs: {sample['pmid']}")
        payload, model = load_payload(sample, MODELS[sample["pmid"]])
        old = old_preview(sample, payload, **options)
        new = repair(sample, payload, **options)
        results.append({"pmid": sample["pmid"], "group_id": sample["group_id"], "sample_sha256": sample["sample_sha256"],
                        "original_export_ready": bool(sample["original_export"].get("offline_ready")),
                        "old_preview": old, "new_preview": new, "model": model})
    knowledge, exports = read(YIELD / "final/knowledge.json"), read(YIELD / "final/tumor-api-export.json")
    if exports["source_bundle_sha256"] != export_digest(knowledge):
        raise ValueError("Historical positive control export differs from its source bundle")
    prior_records = {row["group_id"]: row for row in knowledge["records"]}
    prior_exports = {row["group_id"]: row for row in exports["records"]}
    for group in CONTROL_GROUPS:
        record, previous = prior_records[group], prior_exports[group]
        if not previous.get("offline_ready"):
            raise ValueError("Historical control was not previously structure-ready")
        # The default preservation branch returns these exact old exports and
        # does not require or synthesize a new model result.
        sample = {"record": record, "original_export": previous}
        new = repair(sample, None, **options)
        if new["status"] != "baseline_preserved" or new["original_export"] != previous:
            raise ValueError("Historical ready control was not preserved")
        results.append({"pmid": str(record["article"]["pmid"]), "group_id": group, "historical_control": True,
                        "original_export_ready": True, "old_preview": {"status": "baseline_preserved"}, "new_preview": new,
                        "model": {"status": "reused_existing_control", "seconds": 0}})
    historic = {core_key(row) for row in exports["records"] if row.get("offline_ready")}
    for row in queue_rows(DEFAULT_ROOT):
        if row.get("status") != "offline_ready":
            continue
        _, batch_exports, _, _ = verified_batch(DEFAULT_ROOT, {"id": row["batch_id"]})
        match = next(r for r in batch_exports if r["group_id"] == row["group_id"])
        historic.add(core_key(match))
    historic.discard(None)
    repaired = [row for row in results if row["new_preview"]["status"] == "offline_structure_only"]
    keys = [core_key(row["new_preview"]["derived_export"]) for row in repaired]
    unique = {key for key in keys if key is not None}
    old_counts = sum(row["original_export_ready"] for row in results)
    v1 = sum(row["old_preview"]["status"] in {"baseline_preserved", "offline_structure_only"} for row in results)
    v2 = sum(row["new_preview"]["status"] in {"baseline_preserved", "offline_structure_only"} for row in results)
    return {"version": "biomed.entity-scope-trial.v1", "sample_count": len(results),
            "baseline_offline_count": old_counts, "previous_preview_offline_count": v1, "repair_offline_count": v2,
            "new_offline_candidates": len(repaired), "distinct_new_core_keys": len(unique),
            "not_in_known_historical_ready_keys": sum(key not in historic for key in unique),
            "duplicates_note": "Exact coarse key is a conservative hint; clinical equivalence, cross-source dedup and semantic correctness need reference review.",
            "quality_reference_status": "awaiting_independent_astra_source_review",
            "automatic_clinical_matching": False, "target_contacted": False, "target_written": False,
            "dictionary_sha256": dictionary_sha, "gene_standard_sha256": standard["source"]["sha256"],
            "source_only_sha256": sample_manifest["source_only_sha256"],
            "repair_code_sha256": sha(PROJECT / "apps/curation/source_linked_target_repair.py"),
            "history_ready_key_count": len(historic), "reasons": dict(Counter(r["new_preview"]["reason"] for r in results)),
            "adaptation_seconds": round(time.perf_counter() - started, 3), "records": results}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "comparison-v1.json")
    output = parser.parse_args().output
    result = evaluate()
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({k: result[k] for k in ("sample_count", "baseline_offline_count", "previous_preview_offline_count",
          "repair_offline_count", "new_offline_candidates", "distinct_new_core_keys",
          "not_in_known_historical_ready_keys", "reasons", "adaptation_seconds")}, ensure_ascii=False))
