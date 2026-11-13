"""Run resumable 9B entities and Bonsai relations from completed source screening.

All model outputs come from the preceding stage's immutable inputs. Legacy
answers are never copied into a new article's entity inventory. Preparation
commands do not load models; ``run --import-to-local`` explicitly appends the
result to biomed and reuses local identity rules without publishing to a target.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from scripts.benchmark_knowledge_candidates import (
    completed_results,
    load_inputs,
)
from scripts.classify_literature import atomic_json
from scripts.light_entity_pilot import (
    KINDS,
    locate,
    passage_prompt,
    runner_row,
)
from scripts.prepare_knowledge_intake import freeze, read, sha
from scripts.prepare_knowledge_workflow import (
    relation_prompt,
    validate_packet,
)

VERSION = "minimal-two-stage-v3"
DEFAULT_ROOT = ROOT / "var/experiments/minimal-e2e-20261101/stages"
ENTITY_DEFINITION = ROOT / "var/experiments/lean-entities-20261029/definition.json"
RELATION_DEFINITION = ROOT / "apps/curation/knowledge_relation_prompt.py"
CLINICAL_RELATION_DEFINITION = ROOT / "apps/curation/knowledge_relation_clinical_prompt.py"
LEGACY_RELATION_PROFILE = "independent-anchor-facts-v4"
CLINICAL_RELATION_PROFILE = "target-clinical-focus-v1"
RELATION_PROFILES = (LEGACY_RELATION_PROFILE, CLINICAL_RELATION_PROFILE)
MODEL_PYTHON = Path("/appdata/02_resource/apps/biomed/research/runtime/bin/python")
BONSAI_RUNTIME = Path("/appdata/02_resource/apps/biomed/research/runtime-bonsai")
SCREENING_MODEL = Path("/data/models/Qwen3.5-9B")
SCREENING_MODEL_ID = "Qwen/Qwen3.5-9B"
SCREENING_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
GPU_UUID = "GPU-d2d06c4a-e881-8d70-a0d7-ed65b1010bf2"


def relation_configuration(root, requested=None):
    """An omitted profile inherits frozen work; changes require a new batch."""
    path = Path(root) / "pipeline-manifest.json"
    previous = read(path) if path.exists() else None
    frozen = previous.get("relation_profile", LEGACY_RELATION_PROFILE) if previous else None
    screening_profile = Path(root) / "screening/profile.json"
    if previous is None and screening_profile.exists():
        frozen = read(screening_profile)["relation_profile"]
    profile = requested or frozen or LEGACY_RELATION_PROFILE
    if profile not in RELATION_PROFILES:
        raise ValueError("Unknown relation profile")
    if frozen and profile != frozen:
        raise ValueError("Relation profile changed; use a new pipeline version")
    return profile


def relation_definition(profile):
    if profile == CLINICAL_RELATION_PROFILE:
        from apps.curation.knowledge_relation_clinical_prompt import DEFINITION
        return deepcopy(DEFINITION), CLINICAL_RELATION_DEFINITION
    if profile != LEGACY_RELATION_PROFILE:
        raise ValueError("Unknown relation profile")
    from apps.curation.knowledge_relation_prompt import DEFINITION
    return deepcopy(DEFINITION), RELATION_DEFINITION


def prepare_screening_profile(root, relation_profile=None):
    """Append a new definition beside frozen source inputs; never relabel old output."""
    from apps.curation.knowledge_screening_clinical_prompt import definition_from_legacy

    root = Path(root)
    base = root / "screening"
    profile = relation_configuration(root, relation_profile)
    original = read(base / "definition.json")
    definition = definition_from_legacy(original) if profile == CLINICAL_RELATION_PROFILE else original
    manifest_path = base / "qwen35-9b/manifest.json"
    if manifest_path.exists():
        previous = read(manifest_path)["config"].get("definition")
        if previous != definition:
            raise ValueError("Screening definition changed; use a new batch, preserve existing checkpoints")
    if profile == CLINICAL_RELATION_PROFILE:
        active = base / "clinical-profile"
        freeze(active / "definition.json", definition)
        freeze(active / "inputs.json", read(base / "inputs.json"))
        freeze(base / "profile.json", {"relation_profile": profile,
                                       "definition": "clinical-profile/definition.json",
                                       "source_definition_sha256": digest(original),
                                       "active_definition_sha256": digest(definition),
                                       "inputs_sha256": sha(base / "inputs.json")})
        return active
    return base


def pubtator_configuration(root, enabled=None, cache_root=None):
    """Enable the reference lane for new runs; preserve frozen legacy resumes."""
    from apps.curation.pubtator import CACHE_ROOT
    from apps.curation.pubtator import VERSION as REFERENCE_VERSION

    path = Path(root) / "pipeline-manifest.json"
    prior = read(path) if path.exists() else None
    previous = prior.get("pubtator") if prior else None
    if enabled is None:
        enabled = bool(previous) if prior else True
    if not enabled:
        return None
    cache = cache_root or (previous or {}).get("cache_root") or CACHE_ROOT
    return {"version": REFERENCE_VERSION, "mode": "dual_reference", "cache_root": str(Path(cache).resolve()),
            "skip_local_models": False, "overwrite_local_results": False}


def screen_pipeline(root, *, model_python=MODEL_PYTHON, model=SCREENING_MODEL,
                    model_id=SCREENING_MODEL_ID, revision=SCREENING_REVISION, gpu_uuid=GPU_UUID,
                    relation_profile=None):
    """Run the existing frozen 9B screen; interrupted checkpoints are reusable."""
    from scripts.benchmark_passage_screening import (
        completed_results as screening_results,
    )
    from scripts.benchmark_passage_screening import load_inputs as screening_inputs

    root = Path(root).resolve()
    selection = read(root / "selection.json")
    count = selection["passages"]
    if not count:
        return {"state": "no_screenable_fulltext", "processed": 0,
                "source_statuses": selection.get("source_statuses", [])}
    inputs = screening_inputs(root / "screening/inputs.json", count)
    output = root / "screening/qwen35-9b"
    definition_base = prepare_screening_profile(root, relation_profile)
    command = [str(model_python), "scripts/benchmark_passage_screening.py",
               "--base", str(definition_base), "--output", str(output),
               "--sample-count", str(count), "--model", str(model), "--model-id", model_id,
               "--revision", revision, "--gpu-uuid", gpu_uuid]
    execute_command(command, root / "logs/screening.log")
    manifest, summary = read(output / "manifest.json"), read(output / "summary.json")
    if (manifest["input_sha256"] != digest(inputs) or read(output / "inputs.json") != inputs
            or digest(manifest["config"]) != manifest["config_sha256"]):
        raise ValueError("Screening output does not match the frozen inputs/config")
    completed = screening_results(output, inputs["records"], manifest["config_sha256"])
    if len(completed) != count or summary.get("state") != "completed":
        raise RuntimeError("Screening paused or failed; rerun screen to resume")
    return {"state": "screening_completed", "processed": len(completed),
            "statuses": dict(Counter(row["status"] for row in completed.values()))}


def reuse_screening(source, root):
    """Reference an existing complete screen without copying files or predictions."""
    from scripts.benchmark_passage_screening import (
        completed_results as screened_results,
    )
    from scripts.benchmark_passage_screening import load_inputs as screened_inputs

    source, root = Path(source).resolve(), Path(root).resolve()
    if source == root:
        raise ValueError("Use a new batch directory; the source batch is immutable")
    selection = read(source / "selection.json")
    dataset = screened_inputs(source / "screening/inputs.json", selection["passages"])
    results = source / "screening/qwen35-9b"
    manifest = read(results / "manifest.json")
    if (manifest["input_sha256"] != digest(dataset) or read(results / "inputs.json") != dataset
            or digest(manifest["config"]) != manifest["config_sha256"]):
        raise ValueError("Existing screening does not match its source/config")
    if len(screened_results(results, dataset["records"], manifest["config_sha256"])) != len(dataset["records"]):
        raise ValueError("Existing screening is incomplete")
    freeze(root / "selection.json", selection)
    freeze(root / "screening-reuse.json", {
        "source": str(source), "new_screening_calls": 0,
        "selection_sha256": sha(source / "selection.json"),
        "inputs_sha256": sha(source / "screening/inputs.json"),
        "manifest_sha256": sha(results / "manifest.json"),
    })
    link = root / "screening"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != source / "screening":
            raise ValueError("New batch already has a different screening source")
    else:
        link.symlink_to(source / "screening", target_is_directory=True)
    return {"root": str(root), "screened_passages": len(dataset["records"]), "new_screening_calls": 0}


def check_packets(dataset):
    rows = dataset.get("records")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected nonempty source packets")
    ids = set()
    for packet in rows:
        validate_packet(packet, "entities")
        if packet["id"] in ids:
            raise ValueError("Duplicate source packet ID")
        if packet.get("context_budget_status") == "over_budget":
            raise ValueError("Source packet explicitly exceeds its context budget")
        ids.add(packet["id"])
        version = packet["article"].get("version_id")
        checksum = packet["article"].get("source_sha256")
        if any(p["version_id"] != version or p["source_sha256"] != checksum
               or p.get("pmid", packet["article"]["pmid"]) != packet["article"]["pmid"]
               for p in packet["passages"]):
            raise ValueError("Source packet mixes article versions")
    return rows


def prepare_entities(packets_path, root=DEFAULT_ROOT, max_input_tokens=4096):
    dataset = read(packets_path)
    check_packets(dataset)
    clean = deepcopy(dataset)
    rows = []
    for packet in clean["records"]:
        for field in ("entities", "deferred_entities", "model_entity_ids"):
            packet.pop(field, None)
        packet["article_pmid"] = packet["article"]["pmid"]
        rows.append(runner_row(packet["id"], passage_prompt(packet)))
    output = root / "entities"
    definition = read(ENTITY_DEFINITION)
    freeze(output / "packets.json", clean)
    freeze(output / "inputs.json", {"records": rows})
    freeze(output / "definition.json", definition)
    command = ["python", "scripts/benchmark_knowledge_candidates.py", "--inputs", str(output / "inputs.json"),
               "--definition", str(output / "definition.json"), "--output", str(output / "extract"),
               "--max-input-tokens", str(max_input_tokens), "--max-new-tokens", "2048", "--resume"]
    plan = {"version": VERSION, "stage": "entities", "records": len(rows), "state": "prepared_not_run",
            "source_packets_sha256": sha(packets_path), "inputs_sha256": digest({"records": rows}),
            "definition_source": str(ENTITY_DEFINITION.relative_to(ROOT)), "definition_sha256": digest(definition),
            "inventory_from_prior_answers": False, "new_model_calls": 0, "runner_command": command,
            "input_budget_tokens": max_input_tokens, "output_budget_tokens": 2048, "input_truncation": False,
            "prompt_characters": {r["id"]: len(r["prompt"]) + len(definition["system_prompt"]) for r in rows},
            "budget_policy": "Runner checks actual tokenizer budget; oversized inputs and truncated outputs are failures, never silently shortened.",
            "resume": "Existing identical files are reused; changed source/definition/config requires a new stage directory."}
    freeze(output / "plan.json", plan)
    load_inputs(output / "inputs.json")
    return plan


def project_entity_packet(packet, result, linker=None):
    """Assemble only actual JSON entities; do not repair truncation or invent names."""
    result_packet = deepcopy(packet)
    payload = result.get("payload")
    valid = result.get("status") == "classified" and isinstance(payload, dict) and set(payload) == {"entities"} and isinstance(payload["entities"], list)
    rows = payload["entities"] if valid else []
    issues, entities, seen = [], [], {}
    if not valid:
        issues.append({"reason": "entity_model_failed_or_invalid_schema", "status": result.get("status")})
    for pos, row in enumerate(rows):
        eid = f"{packet['id']}.e{pos + 1}"
        if (not isinstance(row, dict) or set(row) != {"type", "text"}
                or not isinstance(row.get("type"), str) or row["type"] not in KINDS
                or not isinstance(row.get("text"), str) or not row["text"].strip()):
            issues.append({"reason": "invalid_entity", "index": pos, "raw": row})
            continue
        key = (row["type"], row["text"])
        if key in seen:
            seen[key]["raw_indices"].append(pos)
            continue
        locations = locate(packet, row["text"])
        entity = {"id": eid, **row, "raw_indices": [pos], "locations": locations,
                  "status": "located" if locations else "unlocated", "roles": ["context"]}
        if locations and linker:
            entity.update(linker(entity, packet))
        else:
            entity["link"] = {"candidate_id": None, "status": "unresolved", "method": "not_located" if not locations else "not_linked"}
        if not locations:
            issues.append({"reason": "unlocated_entity", "entity_id": eid, "text": row["text"]})
        entities.append(entity)
        seen[key] = entity
    result_packet["entities"] = entities
    result_packet["entity_stage"] = {"model_status": result.get("status"), "schema_valid": valid,
                                     "execution_status": "failed" if not valid or not entities else "partial" if issues else "complete",
                                     "input_sha256": result.get("input_sha256"),
                                     "config_sha256": result.get("config_sha256"),
                                     "result_sha256": digest(result), "issues": issues,
                                     "located_entities": sum(bool(e["locations"]) for e in entities)}
    return result_packet


def checked_entity_results(root):
    entity_root = root / "entities"
    dataset = load_inputs(entity_root / "inputs.json")
    output = entity_root / "extract"
    manifest = read(output / "manifest.json")
    definition = read(entity_root / "definition.json")
    if (manifest["input_sha256"] != digest(dataset)
            or read(output / "inputs.json") != dataset
            or digest(manifest["config"]) != manifest["config_sha256"]
            or manifest["definition_sha256"] != digest(definition)
            or manifest["config"]["definition"] != definition):
        raise ValueError("Entity source/definition/config differs from actual model manifest")
    results = completed_results(output, dataset["records"], manifest["config_sha256"])
    if len(results) != len(dataset["records"]):
        raise ValueError("Entity inference is incomplete; resume the existing runner")
    return results, manifest


def assemble_entities(root=DEFAULT_ROOT):
    from scripts.minimal_knowledge_pilot import standard_linker

    source = read(root / "entities/packets.json")
    check_packets(source)
    results, manifest = checked_entity_results(root)
    if set(results) != {p["id"] for p in source["records"]}:
        raise ValueError("Entity results differ from source packets")
    linker = standard_linker()
    packets = {**deepcopy(source), "records": [project_entity_packet(p, results[p["id"]], linker) for p in source["records"]]}
    report = {"version": VERSION, "stage": "entity_assembly", "records": len(packets["records"]),
              "source_packets_sha256": sha(root / "entities/packets.json"),
              "model_manifest_sha256": sha(root / "entities/extract/manifest.json"),
              "model_config_sha256": manifest["config_sha256"],
              "packets_sha256": digest(packets), "extra_model_calls": 0,
              "states": {p["id"]: p["entity_stage"] for p in packets["records"]}}
    freeze(root / "entities/assembled-packets.json", packets)
    freeze(root / "entities/assembly.json", report)
    return {"records": report["records"], "output": str(root / "entities/assembled-packets.json"),
            "complete": sum(p["entity_stage"]["execution_status"] == "complete" for p in packets["records"]),
            "partial": sum(p["entity_stage"]["execution_status"] == "partial" for p in packets["records"]),
            "failed": sum(p["entity_stage"]["execution_status"] == "failed" for p in packets["records"])}


def relation_inputs(dataset):
    """Unlocated extras stay in lineage; no located inventory means deferred work."""
    check_packets(dataset)
    rows, eligible, deferred = [], [], []
    for packet in dataset["records"]:
        if packet.get("entity_stage", {}).get("model_status") != "classified" or not any(e.get("locations") for e in packet.get("entities", [])):
            deferred.append({"id": packet["id"], "reason": "no_completed_located_entity_inventory",
                             "entity_stage": packet.get("entity_stage", {})})
            continue
        retained, extras = validate_packet(packet, "relations")
        item = deepcopy(packet)
        item["model_entity_ids"] = [e["id"] for e in retained]
        item["deferred_entities"] = extras
        # The original full inventory is retained in packets; only located hints are sent.
        rows.append(runner_row(packet["id"], relation_prompt({**packet, "entities": retained})))
        eligible.append(item)
    return {**deepcopy(dataset), "records": eligible}, {"records": rows}, deferred


def prepare_relations(root=DEFAULT_ROOT, max_input_tokens=4096, relation_profile=None):
    source_path = root / "entities/assembled-packets.json"
    source = read(source_path)
    report = read(root / "entities/assembly.json")
    if digest(source) != report["packets_sha256"]:
        raise ValueError("Assembled entity inventory changed")
    _, manifest = checked_entity_results(root)
    if manifest["config_sha256"] != report["model_config_sha256"]:
        raise ValueError("Assembled inventory model changed")
    packets, inputs, deferred = relation_inputs(source)
    profile = relation_configuration(root.parent, relation_profile)
    definition, definition_source = relation_definition(profile)
    output = root / "relations"
    freeze(output / "all-packets.json", source)
    freeze(output / "packets.json", packets)
    freeze(output / "inputs.json", inputs)
    freeze(output / "definition.json", definition)
    plan = {"version": VERSION, "stage": "relations", "state": "prepared_not_run",
            "source_records": len(source["records"]), "prepared_records": len(inputs["records"]),
            "deferred_records": deferred, "source_packets_sha256": sha(source_path),
            "definition_source": str(definition_source.relative_to(ROOT)), "definition_sha256": digest(definition),
            "model_inventory": "Actual new 9B outputs plus existing exact location/dictionary/guard scripts; no historical answers.",
            "new_model_calls": 0, "input_budget_tokens": max_input_tokens, "output_budget_tokens": 3072, "input_truncation": False,
            "prompt_characters": {r["id"]: len(r["prompt"]) + len(definition["system_prompt"]) for r in inputs["records"]},
            "runner_command": ["python", "scripts/benchmark_bonsai_stage.py", "--root", str(output),
                               "--server", "<existing Prism llama-server path>", "--max-input-tokens", str(max_input_tokens), "--max-new-tokens", "3072"],
            "assembly_command": ["python", "scripts/minimal_knowledge_pilot.py", "assemble", "--root", str(output), "--mode", "inventory"],
            "deferred_policy": "Failed entity stages remain in all-packets and plan; they are not counted as successful extraction or negative knowledge."}
    freeze(output / "plan.json", plan)
    if inputs["records"]:
        load_inputs(output / "inputs.json")
    return plan


def artifact_hashes(paths):
    return {str(path): sha(Path(path)) for path in paths}


def select_batch(dataset, per_document_limit=None, pmids=None, *, allow_absent=False):
    """Fixed first-N per article, before extraction; failures never get replaced."""
    if per_document_limit is not None and (type(per_document_limit) is not int or per_document_limit < 1):
        raise ValueError("Per-document limit must be a positive integer")
    counts, selected, pending = Counter(), [], []
    all_records = dataset["records"]
    requested = set(pmids or [])
    available = {p["article_pmid"] for p in all_records}
    if requested - available and not allow_absent:
        raise ValueError(f"Requested articles have no eligible positive packets: {sorted(requested - available)}")
    article_order = list(dict.fromkeys(p["article_pmid"] for p in all_records))
    ordered = []
    for pmid in article_order:
        article_packets = [p for p in all_records if p["article_pmid"] == pmid]
        article_packets.sort(key=lambda p: next(s["ordinal"] for s in p["passages"] if s["id"] == p["anchor_id"]))
        ordered.extend(article_packets)
    pending_reasons = {}
    for packet in ordered:
        pmid = packet["article_pmid"]
        if packet.get("context_budget_status") == "over_budget":
            pending.append(packet["id"])
            pending_reasons[packet["id"]] = "context_characters_over_budget"
        elif (not requested or pmid in requested) and (per_document_limit is None or counts[pmid] < per_document_limit):
            selected.append(packet)
            counts[pmid] += 1
        else:
            pending.append(packet["id"])
            pending_reasons[packet["id"]] = "article_not_selected" if requested and pmid not in requested else "per_document_limit"
    plan = {"version": VERSION, "source_packets_sha256": digest(dataset),
            "total_positive_packets": len(all_records), "selected_packets": len(selected),
            "per_document_limit": per_document_limit, "selected_by_article": dict(counts),
            "requested_pmids": sorted(requested),
            "pending_selected_article_packets": sum(p["id"] in pending and (not requested or p["article_pmid"] in requested) for p in ordered),
            "all_packet_ids": [p["id"] for p in ordered], "selected_packet_ids": [p["id"] for p in selected],
            "pending_packet_ids": pending, "pending_reasons": pending_reasons, "remaining_packets": len(pending),
            "selection_policy": "Article order from frozen sources, then original ANCHOR ordinal; first N positives per article before entity/relation outputs; no replacement for failures.",
            "continuation": "Keep the full grouped packets; another batch needs a new batch directory/version. Re-running this batch reuses the same selected IDs."}
    return {**deepcopy(dataset), "records": deepcopy(selected)}, plan


def prepare_batch(grouped, selected, per_document_limit, pmids=None, *, allow_absent=False):
    packets, plan = select_batch(read(grouped / "packets.json"), per_document_limit, pmids, allow_absent=allow_absent)
    freeze(selected / "packets.json", packets)
    freeze(selected / "plan.json", plan)


def run_step(state, state_path, name, action, *, inputs, outputs):
    """Checkpoint a real dependency edge; never infer success from process launch."""
    try:
        incoming = artifact_hashes(inputs)
    except OSError as exc:
        state.update(status="failed", current_stage=name, last_error=f"{type(exc).__name__}: {exc}")
        atomic_json(state_path, state)
        raise
    previous = state.setdefault("stages", {}).get(name, {})
    if previous.get("inputs") and previous["inputs"] != incoming:
        state.update(status="failed", current_stage=name, last_error=f"{name}: dependency changed")
        atomic_json(state_path, state)
        raise ValueError(f"{name}: dependency changed; use a new pipeline version")
    if previous.get("status") == "completed":
        if previous["outputs"] != artifact_hashes(outputs):
            state.update(status="failed", current_stage=name, last_error=f"{name}: completed output changed")
            atomic_json(state_path, state)
            raise ValueError(f"{name}: completed output changed")
        return "reused"
    state["status"], state["current_stage"] = "running", name
    state["stages"][name] = {"status": "running", "inputs": incoming,
                              "started_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(state_path, state)
    try:
        action()
        outgoing = artifact_hashes(outputs)  # Missing outputs are a failed stage.
    except BaseException as exc:
        state["status"] = "paused" if isinstance(exc, KeyboardInterrupt) else "failed"
        state["stages"][name].update(status=state["status"], error=f"{type(exc).__name__}: {exc}")
        atomic_json(state_path, state)
        raise
    state["stages"][name].update(status="completed", outputs=outgoing,
                                  completed_at=datetime.now(timezone.utc).isoformat())
    atomic_json(state_path, state)
    return "completed"


def execute_command(command, logfile, env=None):
    """No shell expansion or detached child; exit status determines completion."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("a") as stream:
        stream.write(json.dumps({"command": command, "started_at": datetime.now(timezone.utc).isoformat()}) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"Subprocess exited {result.returncode}; see {logfile}")


def require_model_completion(output, inputs, definition, *, allow_input_too_long=False):
    dataset, spec = load_inputs(inputs), read(definition)
    manifest, summary = read(output / "manifest.json"), read(output / "summary.json")
    if (manifest["input_sha256"] != digest(dataset) or read(output / "inputs.json") != dataset
            or manifest["definition_sha256"] != digest(spec)
            or digest(manifest["config"]) != manifest["config_sha256"]):
        raise ValueError("Model completion does not match prepared inputs/config")
    results = completed_results(output, dataset["records"], manifest["config_sha256"])
    if len(results) != len(dataset["records"]) or summary.get("state") != "completed":
        raise RuntimeError("Model stage stopped before every input had a terminal result")
    oversized = [{"id": rid, "input_tokens": result["input_tokens"]} for rid, result in results.items()
                 if result["status"] == "input_too_long"]
    if oversized:
        atomic_json(output.parent / "input-budget-failures.json", {"limit": manifest["config"]["max_input_tokens"], "records": oversized})
        if not allow_input_too_long:
            raise RuntimeError(f"Explicit input budget exceeded: {oversized}; no truncation or automatic budget change")
    return results


def article_outcomes(selection, grouping, batch, records, packets=None, entity_packets=None):
    """Account for every requested article, including sources that were not screenable."""
    sources = selection.get("source_statuses") or [
        {"pmid": pmid, "status": "ready_for_screening"} for pmid in selection.get("selected_pmids", [])]
    screens = {row["pmid"]: row for row in grouping.get("articles", [])}
    # Earlier frozen packet reports predate article summaries. Their preserved
    # packets and unresolved lists are sufficient to reconstruct these counts.
    if not screens and packets is not None:
        for source in sources:
            pmid = source["pmid"]
            article_packets = [row for row in packets if row.get("article_pmid") == pmid]
            screens[pmid] = {"pmid": pmid, "positive_packets": len(article_packets),
                             "unresolved_passage_ids": [row["id"] for row in grouping.get("deferred_screening", []) if row.get("pmid") == pmid],
                             "over_budget_packet_ids": [row["id"] for row in article_packets if row.get("context_budget_status") == "over_budget"]}
    failed, partial = Counter(), {}
    for record in records:
        status = record.get("assessment", {}).get("execution", {}).get("status")
        pmid = record.get("article", {}).get("pmid")
        if status == "failed":
            failed[pmid] += 1
        elif status == "partial":
            partial.setdefault(pmid, set()).add(record["group_id"])
    for packet in entity_packets or []:
        if packet.get("entity_stage", {}).get("execution_status") == "partial":
            partial.setdefault(packet["article_pmid"], set()).add(packet["id"])
    outcomes = []
    requested = set(batch.get("requested_pmids", []))
    for source in sources:
        pmid = source["pmid"]
        item = {"pmid": pmid, "source": source, "screening": screens.get(pmid),
                "selected_packets": batch.get("selected_by_article", {}).get(pmid, 0),
                "failed_extractions": failed[pmid], "partial_extractions": len(partial.get(pmid, set()))}
        if source["status"] != "ready_for_screening":
            item["status"] = source["status"]
        elif requested and pmid not in requested:
            item["status"] = "not_selected"
        elif item["screening"] is None:
            item["status"] = "screening_not_run"
        else:
            screen = item["screening"]
            incomplete = (screen["unresolved_passage_ids"] or screen["over_budget_packet_ids"]
                          or failed[pmid] or item["partial_extractions"] or item["selected_packets"] < screen["positive_packets"])
            if incomplete:
                item["status"] = "deferred_or_partial"
            elif not screen["positive_packets"]:
                item["status"] = "no_eligible_positive"
            else:
                item["status"] = "processed"
        outcomes.append(item)
    return outcomes


def deliver(root):
    from apps.curation.knowledge_export import export_bundle
    from apps.curation.minimal_knowledge import build_bundle
    from scripts.prepare_minimal_e2e import deduplicate_exact_facts

    stages, output = root / "stages", root / "delivery"
    plan = read(stages / "relations/plan.json")
    all_packets = read(stages / "relations/all-packets.json")["records"]
    bundle = read(stages / "relations/bundle.json") if plan["prepared_records"] else {
        "schema_version": "knowledge-candidates-v1", "source_run": root.name, "provenance": {}, "records": []}
    failed_ids = {row["id"] for row in plan["deferred_records"]}
    failed_packets = [p for p in all_packets if p["id"] in failed_ids]
    failures = {p["id"]: {"status": "upstream_entities_failed", "payload": None,
                           "input_sha256": p["entity_stage"].get("input_sha256"),
                           "upstream_entity_stage": p["entity_stage"]} for p in failed_packets}
    failed_bundle, _ = build_bundle(failed_packets, failures, source_run=root.name, mode="inventory")
    bundle["records"].extend(failed_bundle["records"])
    by_id = {}
    for record in bundle["records"]:
        parent = record.get("provenance", {}).get("source_packet_id", record["group_id"])
        by_id.setdefault(parent, []).append(record)
    if len({r["group_id"] for r in bundle["records"]}) != len(bundle["records"]):
        raise ValueError("Duplicate delivered fact identity")
    if set(by_id) != {p["id"] for p in all_packets}:
        raise ValueError("Delivery does not account for every positive source packet")
    bundle["records"] = [record for p in all_packets for record in by_id[p["id"]]]
    bundle["provenance"].update(pipeline=VERSION, screening_manifest_sha256=sha(root / "screening/qwen35-9b/manifest.json"),
                                failed_entity_stages=plan["deferred_records"], semantic_review_is_independent=True)
    all_statuses = Counter(r["assessment"]["execution"]["status"] for r in bundle["records"])
    reduced, dedup = deduplicate_exact_facts(bundle)
    preliminary = export_bundle(reduced)
    for record, exported in zip(reduced["records"], preliminary["records"], strict=True):
        record["assessment"]["compatibility"] = {
            "status": exported["compatibility_status"], "issues": exported["mapping_issues"],
            "contract_validation": exported["contract_validation"], "submit_ready": False}
    package = export_bundle(reduced)
    selection, grouping = read(root / "selection.json"), read(root / "grouped/packet-preparation.json")
    batch = read(root / "selected-batch/plan.json")
    outcomes = article_outcomes(selection, grouping, batch, bundle["records"], read(root / "grouped/packets.json")["records"], all_packets)
    deferred = any(item["status"] not in {"processed", "no_eligible_positive"} for item in outcomes)
    summary = {"version": VERSION, "state": "batch_completed_with_deferred" if deferred else "batch_completed", "articles": selection["articles"],
               "article_outcomes": outcomes,
               "screened_passages": selection["passages"], "positive_source_packets": batch["total_positive_packets"],
               "batch_packets": len(all_packets), "remaining_packets": batch["remaining_packets"],
               "pending_packet_ids": batch["pending_packet_ids"], "per_document_limit": batch["per_document_limit"],
               "pending_reasons": batch.get("pending_reasons", {}),
               "requested_pmids": batch["requested_pmids"], "selected_by_article": batch["selected_by_article"],
               "pending_selected_article_packets": batch["pending_selected_article_packets"],
               "screening_unresolved": grouping["deferred_screening"],
               "execution_before_dedup": dict(all_statuses), "entity_stage_failed": len(failed_packets),
               "relation_stage_inputs": plan["prepared_records"], "deduplication": dedup,
               "delivered_records": len(reduced["records"]), "compatibility_counts": package["counts"],
               "quality": "unreviewed; evaluation is independent", "database_written": False,
               "target_written": False, "published": False, "submit_ready": False}
    freeze(output / "bundle.json", reduced)
    freeze(output / "tumor-api-export.json", package)
    freeze(output / "summary.json", summary)


def deliver_empty(root, selection, grouping, batch):
    """Persist an explicit empty result without inventing model decisions."""
    from apps.curation.knowledge_export import export_bundle

    bundle = {"schema_version": "knowledge-candidates-v1", "source_run": root.name,
              "provenance": {"pipeline": VERSION, "reason": "no_packets_eligible_for_inference"}, "records": []}
    outcomes = article_outcomes(selection, grouping, batch, [], read(root / "grouped/packets.json")["records"] if grouping.get("state") != "no_screenable_fulltext" else None)
    deferred = any(item["status"] != "no_eligible_positive" for item in outcomes)
    summary = {"version": VERSION, "state": "batch_completed_with_deferred" if deferred else "batch_completed",
               "articles": selection["articles"], "article_outcomes": outcomes,
               "screened_passages": selection["passages"], "positive_source_packets": batch["total_positive_packets"],
               "batch_packets": 0, "remaining_packets": batch["remaining_packets"],
               "pending_packet_ids": batch["pending_packet_ids"], "pending_reasons": batch.get("pending_reasons", {}),
               "screening_unresolved": grouping.get("deferred_screening", []), "delivered_records": 0,
               "database_written": False, "target_written": False, "published": False, "submit_ready": False,
               "reason": "no_packets_eligible_for_inference"}
    freeze(root / "delivery/bundle.json", bundle)
    freeze(root / "delivery/tumor-api-export.json", export_bundle(bundle))
    freeze(root / "delivery/summary.json", summary)
    return summary


def persist_delivery(root):
    """Explicit local import; the database service also applies confirmed aliases.

    A process failure never creates a receipt. If a process committed but its
    receipt was interrupted, the existing import's content digest makes replay
    safe. This cannot publish or contact the target project.
    """
    bundle_path = root / "delivery/bundle.json"
    with bundle_path.open("rb") as source:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "web", "python", "manage.py",
             "import_knowledge_candidates", "--input", "/dev/stdin"],
            cwd=ROOT, stdin=source, capture_output=True, check=False,
        )
    if result.returncode:
        raise RuntimeError(f"Local knowledge import failed: {result.stderr.decode(errors='replace')[-2000:]}")
    receipt = json.loads(result.stdout)
    if receipt.get("dry_run") or "entity_registry" not in receipt:
        raise ValueError("Local import did not confirm automatic entity-registry integration")
    freeze(root / "delivery/import-receipt.json", {
        "bundle_sha256": sha(bundle_path), "target_written": False,
        "published": False, "database": "biomed-local", "result": receipt,
    })


def reassemble_pipeline(source, root, *, import_to_local=False):
    """Append a derived assembly from the exact existing inference, with no GPU."""
    source, root = Path(source).resolve(), Path(root).resolve()
    if source == root:
        raise ValueError("Reassembly requires a new version directory")
    parent = read(source / "pipeline-state.json")
    if parent.get("status") not in {"batch_completed", "batch_completed_with_deferred"}:
        raise ValueError("Source pipeline must have completed")
    relation_source = source / "stages/relations"
    if not (relation_source / "plan.json").exists():
        raise ValueError("Source batch has no inference to reassemble")
    relation_count = read(relation_source / "plan.json")["prepared_records"]
    if relation_count:
        require_model_completion(relation_source / "extract", relation_source / "inputs.json", relation_source / "definition.json",
                                 allow_input_too_long=True)
    methods = [ROOT / path for path in (
        "apps/curation/minimal_knowledge.py", "apps/curation/compound_entity_mentions.py", "apps/curation/source_entity_types.py",
        "apps/curation/knowledge_eligibility.py", "scripts/run_minimal_pipeline.py",
    )]
    manifest = {"version": "minimal-derived-assembly-v1", "parent": str(source),
                "parent_bundle_sha256": sha(source / "delivery/bundle.json"),
                "parent_state_sha256": sha(source / "pipeline-state.json"),
                "inputs_sha256": sha(relation_source / "inputs.json"),
                "model_manifest_sha256": sha(relation_source / "extract/manifest.json") if relation_count else None,
                "methods": artifact_hashes(methods), "model_calls": 0,
                "import_to_local": import_to_local}
    freeze(root / "derivation-manifest.json", manifest)
    paths = ["selection.json", "screening", "grouped", "selected-batch"]
    paths += ["stages/relations/" + name for name in (
        "inputs.json", "definition.json", "packets.json", "all-packets.json", "plan.json",
    )]
    if relation_count:
        paths.append("stages/relations/extract")
    for name in paths:
        original, link = source / name, root / name
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or link.resolve() != original.resolve():
                raise ValueError(f"Derived source differs: {name}")
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(original.resolve(), target_is_directory=original.is_dir())
    if relation_count:
        execute_command([sys.executable, "scripts/minimal_knowledge_pilot.py", "assemble",
                         "--root", str(root / "stages/relations"), "--mode", "inventory",
                         "--source-run", root.name], root / "logs/assembly.log")
    deliver(root)
    if import_to_local:
        persist_delivery(root)
    summary = {**read(root / "delivery/summary.json"), "database_written": import_to_local,
               "model_calls": 0, "parent_source_run": source.name}
    freeze(root / "derivation-result.json", summary)
    return summary


def run_pipeline(root, *, model_python=MODEL_PYTHON,
                 server=BONSAI_RUNTIME / "build-sm75/bin/llama-server",
                 cuda_lib=BONSAI_RUNTIME / "cuda-12.4/lib", per_document_limit=None,
                 pmids=None, import_to_local=False, max_context_characters=12000,
                 max_input_tokens=4096, pubtator=None, pubtator_cache=None, relation_profile=None):
    from scripts.prepare_minimal_e2e import prepare_packets

    root = Path(root).resolve()
    if not (root / "selection.json").is_file():
        raise ValueError("Prepare and finish full-text screening first")
    if type(max_context_characters) is not int or max_context_characters < 1:
        raise ValueError("Context character budget must be positive")
    if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= 5120:
        raise ValueError("Input token budget must be 1..5120 (Bonsai context is 8192 with 3072 output tokens)")
    selection = read(root / "selection.json")
    missing_request = set(pmids or []) - set(selection.get("requested_pmids", selection["selected_pmids"]))
    if missing_request:
        raise ValueError(f"Requested PMIDs are not in the frozen selection: {sorted(missing_request)}")
    state_path = root / "pipeline-state.json"
    with (root / "pipeline.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Distinct from runner GPU lock.
        screening = root / "screening/qwen35-9b"
        profile = relation_configuration(root, relation_profile)
        _, definition_source = relation_definition(profile)
        if profile == CLINICAL_RELATION_PROFILE and selection["passages"]:
            from apps.curation.knowledge_screening_clinical_prompt import (
                VERSION as SCREENING_PROFILE_VERSION,
            )

            if read(screening / "manifest.json")["config"].get("definition", {}).get("version") != SCREENING_PROFILE_VERSION:
                raise ValueError("Clinical relation profile requires its molecular diagnostic screening; use a new batch")
        config = {"version": VERSION, "root": str(root), "model_python": str(model_python),
                  "server": str(server), "cuda_lib": str(cuda_lib), "max_input_tokens": max_input_tokens, "max_new_tokens": 3072,
                  "per_document_limit": per_document_limit,
                  "pmids": sorted(set(pmids or [])), "import_to_local": import_to_local,
                  "screening_inputs_sha256": sha(root / "screening/inputs.json"),
                  "screening_manifest_sha256": sha(screening / "manifest.json") if selection["passages"] else None,
                  "screening_results": artifact_hashes(sorted(screening.glob("result-*.json"))),
                  "entity_definition_sha256": sha(ENTITY_DEFINITION), "relation_definition_sha256": sha(definition_source)}
        if profile != LEGACY_RELATION_PROFILE:
            config["relation_profile"] = profile
            config["relation_base_definition_sha256"] = sha(RELATION_DEFINITION)
        if max_context_characters != 12000:
            config["max_context_characters"] = max_context_characters
        reference_config = pubtator_configuration(root, pubtator, pubtator_cache)
        if reference_config:
            config["pubtator"] = reference_config
        freeze(root / "pipeline-manifest.json", config)
        state = read(state_path) if state_path.exists() else {"version": VERSION, "config_sha256": digest(config), "stages": {}}
        if state["config_sha256"] != digest(config):
            raise ValueError("Pipeline configuration changed; use a new version")
        state["resume_command"] = [sys.executable, str(Path(__file__).resolve()), "run", "--root", str(root),
                                   "--model-python", str(model_python), "--server", str(server), "--cuda-lib", str(cuda_lib)]
        if per_document_limit is not None:
            state["resume_command"] += ["--per-document-limit", str(per_document_limit)]
        for pmid in config["pmids"]:
            state["resume_command"] += ["--pmid", pmid]
        if import_to_local:
            state["resume_command"].append("--import-to-local")
        if max_context_characters != 12000:
            state["resume_command"] += ["--max-context-characters", str(max_context_characters)]
        if max_input_tokens != 4096:
            state["resume_command"] += ["--max-input-tokens", str(max_input_tokens)]
        if reference_config:
            state["resume_command"] += ["--pubtator", "--pubtator-cache", reference_config["cache_root"]]
        if profile != LEGACY_RELATION_PROFILE:
            state["resume_command"] += ["--relation-profile", profile]
        stages, grouped = root / "stages", root / "grouped"

        def step(name, action, inputs, outputs):
            status = run_step(state, state_path, name, action, inputs=inputs, outputs=outputs)
            print(json.dumps({"stage": name, "status": status}), flush=True)

        if selection["passages"]:
            step("group_passages", lambda: prepare_packets(root, screening, grouped, max_context_characters),
                 [root / "selection.json", root / "screening/inputs.json", screening / "manifest.json"],
                 [grouped / "packets.json", grouped / "packet-preparation.json"])
        else:
            def no_screenable_sources():
                freeze(grouped / "packets.json", {"schema_version": "knowledge-candidate-inputs-v1", "documents": [], "records": []})
                freeze(grouped / "packet-preparation.json", {"state": "no_screenable_fulltext", "articles": [],
                                                              "deferred_screening": [], "oversized_packets": []})
            step("group_passages", no_screenable_sources, [root / "selection.json", root / "screening/inputs.json"],
                 [grouped / "packets.json", grouped / "packet-preparation.json"])
        selected = root / "selected-batch"
        step("select_batch", lambda: prepare_batch(grouped, selected, per_document_limit, pmids, allow_absent=True),
             [grouped / "packets.json"], [selected / "packets.json", selected / "plan.json"])
        if not read(selected / "packets.json")["records"]:
            step("deliver_empty", lambda: deliver_empty(root, selection, read(grouped / "packet-preparation.json"), read(selected / "plan.json")),
                 [root / "selection.json", grouped / "packet-preparation.json", selected / "plan.json"],
                 [root / "delivery/bundle.json", root / "delivery/tumor-api-export.json", root / "delivery/summary.json"])
            summary = read(root / "delivery/summary.json")
            state.update(status=summary["state"], current_stage=None, summary=summary)
            atomic_json(state_path, state)
            return summary
        step("prepare_entities", lambda: prepare_entities(selected / "packets.json", stages, max_input_tokens),
             [selected / "packets.json", ENTITY_DEFINITION],
             [stages / "entities/inputs.json", stages / "entities/definition.json", stages / "entities/packets.json", stages / "entities/plan.json"])
        entity_dir = stages / "entities"

        def infer_entities():
            command = read(entity_dir / "plan.json")["runner_command"]
            command[0] = str(model_python)
            execute_command(command, root / "logs/entities.log")
            require_model_completion(entity_dir / "extract", entity_dir / "inputs.json", entity_dir / "definition.json", allow_input_too_long=True)

        step("infer_entities", infer_entities, [entity_dir / "inputs.json", entity_dir / "definition.json"],
             [entity_dir / "extract/manifest.json", entity_dir / "extract/summary.json"])
        step("assemble_entities", lambda: assemble_entities(stages),
             [entity_dir / "packets.json", entity_dir / "extract/manifest.json", *sorted((entity_dir / "extract").glob("result-*.json"))],
             [entity_dir / "assembled-packets.json", entity_dir / "assembly.json"])
        relation_dir = stages / "relations"
        relation_sources = [RELATION_DEFINITION] + ([definition_source] if profile != LEGACY_RELATION_PROFILE else [])
        step("prepare_relations", lambda: prepare_relations(stages, max_input_tokens, profile), [entity_dir / "assembled-packets.json", entity_dir / "assembly.json", *relation_sources],
             [relation_dir / "inputs.json", relation_dir / "definition.json", relation_dir / "packets.json", relation_dir / "all-packets.json", relation_dir / "plan.json"])
        relation_count = read(relation_dir / "plan.json")["prepared_records"]
        if relation_count:
            def infer_relations():
                command = [str(model_python), "scripts/benchmark_bonsai_stage.py", "--root", str(relation_dir), "--server", str(server),
                           "--max-input-tokens", str(max_input_tokens), "--max-new-tokens", "3072"]
                env = {**os.environ, "LD_LIBRARY_PATH": str(cuda_lib) + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else "")}
                execute_command(command, root / "logs/relations.log", env)
                require_model_completion(relation_dir / "extract", relation_dir / "inputs.json", relation_dir / "definition.json", allow_input_too_long=True)

            step("infer_relations", infer_relations, [relation_dir / "inputs.json", relation_dir / "definition.json"],
                 [relation_dir / "extract/manifest.json", relation_dir / "extract/summary.json"])
            command = [sys.executable, "scripts/minimal_knowledge_pilot.py", "assemble", "--root", str(relation_dir),
                       "--mode", "inventory", "--source-run", root.name]
            step("assemble_knowledge", lambda: execute_command(command, root / "logs/assembly.log"),
                 [relation_dir / "packets.json", relation_dir / "extract/manifest.json", *sorted((relation_dir / "extract").glob("result-*.json"))],
                 [relation_dir / "bundle.json", relation_dir / "assembly.json"])
        step("deliver", lambda: deliver(root),
             [relation_dir / "all-packets.json", relation_dir / "plan.json", selected / "plan.json"] + ([relation_dir / "bundle.json"] if relation_count else []),
             [root / "delivery/bundle.json", root / "delivery/tumor-api-export.json", root / "delivery/summary.json"])
        summary = read(root / "delivery/summary.json")
        if import_to_local:
            step("persist_local", lambda: persist_delivery(root),
                 [root / "delivery/bundle.json"], [root / "delivery/import-receipt.json"])
            summary = {**summary, "database_written": True,
                       "entity_registry": read(root / "delivery/import-receipt.json")["result"]["entity_registry"]}
        if reference_config:
            from scripts.pubtator_reference import optional_reference

            # Both local model stages and delivery above always run first.
            # API availability never selects a different extraction branch.
            summary = {**summary, "pubtator": optional_reference(root, cache_root=reference_config["cache_root"])}
            print(json.dumps({"stage": "pubtator_reference", **summary["pubtator"]}), flush=True)
        state.update(status=summary["state"], current_stage=None, summary=summary)
        atomic_json(state_path, state)
        return state["summary"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("screen", help="Run/resume existing 9B paragraph screening for frozen sources")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--model-python", type=Path, default=MODEL_PYTHON)
    p.add_argument("--model", type=Path, default=SCREENING_MODEL)
    p.add_argument("--model-id", default=SCREENING_MODEL_ID)
    p.add_argument("--revision", default=SCREENING_REVISION)
    p.add_argument("--gpu-uuid", default=GPU_UUID)
    p.add_argument("--relation-profile", choices=RELATION_PROFILES)
    p = sub.add_parser("reuse-screening")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("reassemble")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--import-to-local", action="store_true")
    for command in ("prepare-entities", "assemble-entities", "prepare-relations"):
        p = sub.add_parser(command)
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        if command == "prepare-entities":
            p.add_argument("--packets", type=Path, required=True)
        if command == "prepare-relations":
            p.add_argument("--relation-profile", choices=RELATION_PROFILES)
    p = sub.add_parser("run")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT.parent)
    p.add_argument("--model-python", type=Path, default=MODEL_PYTHON)
    p.add_argument("--server", type=Path, default=BONSAI_RUNTIME / "build-sm75/bin/llama-server")
    p.add_argument("--cuda-lib", type=Path, default=BONSAI_RUNTIME / "cuda-12.4/lib")
    p.add_argument("--per-document-limit", type=int, help="Explicit first-N positive anchors per article; omit to process every positive")
    p.add_argument("--pmid", action="append", help="Process every eligible positive for these articles; other articles remain pending")
    p.add_argument("--max-context-characters", type=int, default=12000, help="Packet preparation limit; oversized packets remain pending")
    p.add_argument("--max-input-tokens", type=int, default=4096, help="Model input limit, 1..5120; use a new batch for changes")
    p.add_argument("--import-to-local", action="store_true", help="Append candidates and resolve confirmed local aliases in biomed; no publication or target import")
    p.add_argument("--pubtator", action=argparse.BooleanOptionalAction, default=None,
                   help="Independent cached reference; enabled for new batches, never skips local models")
    p.add_argument("--pubtator-cache", type=Path, help="Shared annotation cache; inherit frozen path on resume")
    p.add_argument("--relation-profile", choices=RELATION_PROFILES,
                   help="Explicit prompt version for new batches; inherit frozen version on resume")
    args = parser.parse_args()
    if args.command == "screen":
        result = screen_pipeline(args.root, model_python=args.model_python, model=args.model,
                                 model_id=args.model_id, revision=args.revision, gpu_uuid=args.gpu_uuid,
                                 relation_profile=args.relation_profile)
    elif args.command == "reuse-screening":
        result = reuse_screening(args.source, args.root)
    elif args.command == "reassemble":
        result = reassemble_pipeline(args.source, args.root, import_to_local=args.import_to_local)
    elif args.command == "prepare-entities":
        result = prepare_entities(args.packets, args.root)
    elif args.command == "assemble-entities":
        result = assemble_entities(args.root)
    elif args.command == "prepare-relations":
        result = prepare_relations(args.root, relation_profile=args.relation_profile)
    else:
        result = run_pipeline(args.root, model_python=args.model_python, server=args.server, cuda_lib=args.cuda_lib,
                              per_document_limit=args.per_document_limit, pmids=args.pmid,
                              import_to_local=args.import_to_local, max_context_characters=args.max_context_characters,
                              max_input_tokens=args.max_input_tokens, pubtator=args.pubtator, pubtator_cache=args.pubtator_cache,
                              relation_profile=args.relation_profile)
    print(json.dumps(result, ensure_ascii=False))
