"""Run a bounded literature list through local classification and knowledge extraction.

The input is one or more verified, immutable literature snapshots. Existing
sources, predictions and deliveries are never overwritten. No target connection,
model download, publication, or production tumor_api import is performed.
"""

import argparse
import copy
import fcntl
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import checked_input, digest
from apps.curation.three_class import validate_output
from scripts.benchmark_knowledge_candidates import completed_results, load_inputs
from scripts.prepare_knowledge_intake import freeze, read, sha
from scripts.prepare_minimal_e2e import freeze_snapshot_sources, selection_pmids
from scripts.run_minimal_pipeline import MODEL_PYTHON, execute_command, run_pipeline

VERSION = "literature-batch-v1"
CLINICAL_PROFILE = "target-clinical-focus-v1"
CLASSIFIER = Path("/data/models/Qwen3.5-4B")
CLASS_DEFINITION = ROOT / "var/experiments/three-class-20261022/task-definition-v1.json"


def selected_documents(snapshots, pmids):
    """Require a unique frozen version, including for metadata-only failures."""
    documents = {}
    for path in snapshots:
        envelope = read(path)
        snapshot = envelope["snapshot"]
        if digest(snapshot) != envelope["sha256"]:
            raise ValueError("Snapshot payload digest mismatch")
        for document in snapshot["documents"]:
            pmid = str(document.get("pmid", ""))
            if pmid in pmids:
                prior = documents.get(pmid)
                if prior is not None and digest(prior) != digest(document):
                    raise ValueError(f"Multiple frozen source versions for PMID {pmid}")
                documents[pmid] = document
    return documents


def prepare_classification(root, snapshots, pmids):
    documents = selected_documents(snapshots, pmids)
    definition = read(CLASS_DEFINITION)
    rows, unavailable = [], []
    for pmid in pmids:
        document = documents.get(pmid)
        source = document.get("model_input") if document else None
        if not source:
            unavailable.append({"pmid": pmid, "status": "metadata_unavailable"})
            continue
        source = checked_input(source)
        if source["pmid"] != pmid:
            raise ValueError("Metadata PMID differs from selected source")
        row = {"id": pmid, "pmid": pmid, "version_id": source["version_id"],
               "source_sha256": source["source_sha256"],
               "metadata_input_sha256": source["input_sha256"],
               "prompt": json.dumps(source["metadata"], ensure_ascii=False)}
        row["input_sha256"] = digest(row)
        rows.append(row)
    folder = root / "classification"
    freeze(folder / "inputs.json", {"records": rows})
    freeze(folder / "definition.json", {"version": definition["task_version"],
                                        "system_prompt": definition["prompt"]})
    freeze(folder / "source-status.json", {"requested_pmids": pmids, "unavailable": unavailable,
                                           "task_definition_sha256": sha(CLASS_DEFINITION)})
    return len(rows)


def classification_selection(root):
    folder = root / "classification"
    status = read(folder / "source-status.json")
    dataset = read(folder / "inputs.json")
    records = {row["pmid"]: row for row in status["unavailable"]}
    if dataset["records"]:
        dataset = load_inputs(folder / "inputs.json")
        output = folder / "extract"
        manifest = read(output / "manifest.json")
        if (manifest["input_sha256"] != digest(dataset)
                or read(output / "inputs.json") != dataset
                or digest(manifest["config"]) != manifest["config_sha256"]
                or manifest["definition_sha256"] != digest(read(folder / "definition.json"))):
            raise ValueError("Classification input/config identity mismatch")
        results = completed_results(output, dataset["records"], manifest["config_sha256"])
        if len(results) != len(dataset["records"]):
            raise ValueError("Classification incomplete; resume the same batch")
        for pmid, result in results.items():
            row = {"pmid": pmid, "status": result["status"], "category": None}
            if result["status"] == "classified":
                try:
                    row["category"] = validate_output(result["raw_output"])
                    if result.get("payload") != {"category": row["category"]}:
                        raise ValueError("Payload and raw output differ")
                    row["status"] = "classified" if row["category"] else "abstained"
                except (ValueError, TypeError):
                    row.update(category=None, status="invalid_category_output")
            records[pmid] = row
    rows = [records[pmid] for pmid in status["requested_pmids"]]
    selected = [row["pmid"] for row in rows if row.get("category") == "solid"]
    summary = {"records": rows, "solid_pmids": selected,
               "categories": dict(Counter(row.get("category") for row in rows)),
               "policy": "Only solid proceeds automatically; every excluded or unfinished article remains in this ledger."}
    freeze(folder / "selection.json", summary)
    return summary


def run_classification(root, model_python):
    count = len(read(root / "classification/inputs.json")["records"])
    if count:
        folder = root / "classification"
        model = read(CLASSIFIER / "download-manifest.json")
        execute_command([str(model_python), "scripts/benchmark_knowledge_candidates.py",
                         "--inputs", str(folder / "inputs.json"),
                         "--definition", str(folder / "definition.json"),
                         "--output", str(folder / "extract"), "--resume",
                         "--model", str(CLASSIFIER), "--model-id", model["repo"],
                         "--revision", model["revision"],
                         "--max-input-tokens", "6144", "--max-new-tokens", "128"],
                        root / "logs/classification.log")
    return classification_selection(root)


def route_with_fulltext(root, snapshots, selection, model_python):
    from scripts.classify_fulltext_fallback import prepare as prepare_fallback
    from scripts.classify_fulltext_fallback import run as run_fallback

    frozen_selection = read(root / "classification/selection.json")
    if (selection.get("records") != frozen_selection.get("records")
            or selection.get("solid_pmids") != frozen_selection.get("solid_pmids")):
        raise ValueError("Classification differs from its frozen selection")
    selection = frozen_selection
    folder = root / "fallback-fulltext-v1"
    if (folder / "routing.json").exists():
        fallback = cached_fulltext_fallback(folder, snapshots, root / "classification/selection.json", selection)
    else:
        prepare_fallback(snapshots, root / "classification/selection.json", folder)
        fallback = run_fallback(folder, model_python=model_python)
    continued = set(selection["solid_pmids"]) | set(fallback["continue_pmids"])
    routed = {**copy.deepcopy(selection),
              "solid_pmids": [row["pmid"] for row in selection["records"] if row["pmid"] in continued],
              "fulltext_fallback": str(folder), "fallback_routing_sha256": sha(folder / "routing.json")}
    freeze(root / "classification-routing.json", routed)
    return effective_routing(root, selection, routed, fallback), fallback


def cached_fulltext_fallback(folder, snapshots, selection_path, selection):
    """Check completed fallback artifacts and reuse them without a GPU lock."""
    plan, fallback = read(folder / "preparation.json"), read(folder / "routing.json")
    if (plan["original_classification_file_sha256"] != sha(selection_path)
            or plan["original_classification_sha256"] != digest(selection)
            or plan["inputs_sha256"] != digest(read(folder / "inputs.json"))
            or plan["definition_sha256"] != digest(read(folder / "definition.json"))
            or fallback["preparation_sha256"] != sha(folder / "preparation.json")):
        raise ValueError("Cached full-text fallback inputs or provenance changed")
    sources = {str(Path(path).resolve()): sha(path) for path in snapshots}
    if sources != {path: value["file_sha256"] for path, value in plan["sources"].items()}:
        raise ValueError("Cached full-text fallback source snapshots changed")
    model_manifest = fallback.get("model_manifest_sha256")
    if model_manifest is not None and model_manifest != sha(folder / "extract/manifest.json"):
        raise ValueError("Cached full-text fallback model manifest changed")
    for row in fallback["records"]:
        if "fallback_category" in row:
            result = read(folder / "extract" / f"result-{row['pmid']}.json")
            if row.get("model_result_sha256") != digest(result):
                raise ValueError("Cached full-text fallback model result changed")
    return fallback


def effective_routing(root, original, routed, fallback):
    """Append a derived ledger while retaining the original metadata labels."""
    fallback_path = root / "fallback-fulltext-v1/routing.json"
    if routed.get("fallback_routing_sha256") != sha(fallback_path) or fallback != read(fallback_path):
        raise ValueError("Full-text routing differs from its frozen provenance")
    if routed.get("records") != original.get("records"):
        raise ValueError("Routing differs from the original classification ledger")
    decisions = {row["pmid"]: row for row in fallback["records"]}
    if len(decisions) != len(fallback["records"]) or set(decisions) != {row["pmid"] for row in original["records"]}:
        raise ValueError("Full-text fallback does not account for every original article")
    recovered = {row["pmid"] for row in fallback["records"]
                 if row["fallback_status"] == "classified" and row.get("fallback_category") == "solid"}
    if len(set(fallback["continue_pmids"])) != len(fallback["continue_pmids"]) or set(fallback["continue_pmids"]) != recovered:
        raise ValueError("Fallback continuation differs from classified solid results")
    expected = set(original["solid_pmids"]) | recovered
    if routed.get("solid_pmids") != [row["pmid"] for row in original["records"] if row["pmid"] in expected]:
        raise ValueError("Routed articles differ from original and fallback classifications")
    rows = []
    for source in original["records"]:
        fallback_row = decisions[source["pmid"]]
        if (fallback_row.get("original_status") != source.get("status")
                or fallback_row.get("original_category") != source.get("category")):
            raise ValueError("Fallback classification is bound to a different original result")
        status, category, scope = source.get("status"), source.get("category"), "metadata"
        fallback_status = fallback_row["fallback_status"]
        if fallback_status == "classified":
            status, category, scope = "classified", fallback_row.get("fallback_category"), "fulltext"
            if category not in {"solid", "hematologic", "non_neoplastic"}:
                raise ValueError("Fallback classified result has an invalid category")
        elif fallback_status not in {"already_solid", "already_hematologic", "not_a_safety_candidate"}:
            status, category, scope = "unresolved", None, "fulltext_fallback_unresolved"
        rows.append({**copy.deepcopy(source), "effective_status": status, "effective_category": category,
                     "classification_scope": scope, "fulltext_fallback": copy.deepcopy(fallback_row)})
    derived = {**copy.deepcopy(routed), "records": rows,
               "original_selection_sha256": sha(root / "classification/selection.json"),
               "base_routing_sha256": sha(root / "classification-routing.json"),
               "effective_categories": dict(Counter(row["effective_category"] or "unresolved" for row in rows))}
    freeze(root / "classification-routing-v2.json", derived)
    return derived


def delivery_classification(root):
    """Use a full-text ledger only when it produced this exact frozen batch."""
    original = read(root / "classification/selection.json")
    source_path, routing_path = root / "selection.json", root / "classification-routing.json"
    if not source_path.exists():
        return original
    source = read(source_path)
    frozen_pmids = source.get("requested_pmids", source.get("selected_pmids", []))
    original_solid = set(original.get("solid_pmids", [row["pmid"] for row in original["records"]
                                                    if row.get("status") == "classified" and row.get("category") == "solid"]))
    routed = read(routing_path) if routing_path.exists() else None
    if not routed or routed.get("solid_pmids") != frozen_pmids:
        if set(frozen_pmids) - original_solid:
            raise ValueError("Frozen articles beyond the original classification require matching full-text routing provenance")
        return original
    fallback_path = root / "fallback-fulltext-v1/routing.json"
    if not fallback_path.exists():
        raise ValueError("Matching full-text routing has no fallback provenance")
    return effective_routing(root, original, routed, read(fallback_path))


def resume_fulltext_fallback(root, snapshots, model_python, dictionary, registry, *, import_to_local=False,
                             per_document_limit=None, clinical_profile=None):
    """Append recovered papers after an already-frozen batch, without rerunning it."""
    from scripts.run_minimal_pipeline import screen_pipeline

    if per_document_limit is not None and (type(per_document_limit) is not int or per_document_limit < 1):
        raise ValueError("Per-document limit must be a positive integer")
    original = read(root / "classification/selection.json")
    _, fallback = route_with_fulltext(root, snapshots, original, model_python)
    pmids = fallback["continue_pmids"]
    if not pmids:
        result = {"state": fallback["state"], "recovered_articles": 0}
        freeze(root / "fulltext-followup-result.json", result)
        return result
    child = root / f"{root.name}-fulltext-followup"
    records = []
    for row in fallback["records"]:
        if row["pmid"] in pmids:
            records.append({"pmid": row["pmid"], "status": row["fallback_status"],
                            "category": row["fallback_category"],
                            "original_category": row["original_category"],
                            "classification_source": str(root / "fallback-fulltext-v1/routing.json")})
    freeze(child / "classification/selection.json", {"records": records, "solid_pmids": pmids,
                                                   "scope": "fulltext_fallback", "parent_source_run": root.name})
    manifest = {"version": VERSION, "pmids": pmids, "parent_source_run": root.name,
                "fallback_routing_sha256": sha(root / "fallback-fulltext-v1/routing.json")}
    if clinical_profile:
        manifest["clinical_profile"] = clinical_profile
    if per_document_limit is not None:
        manifest["per_document_limit"] = per_document_limit
    freeze(child / "batch-manifest.json", manifest)
    freeze_snapshot_sources(child, snapshots, pmids)
    screen_pipeline(child, model_python=model_python,
                    **({"relation_profile": clinical_profile} if clinical_profile else {}))
    result = run_pipeline(child, model_python=model_python, import_to_local=False,
                          per_document_limit=per_document_limit,
                          **({"relation_profile": clinical_profile} if clinical_profile else {}))
    if dictionary:
        result = deliver_batch(child, dictionary, registry, import_to_local=import_to_local,
                               normalize_entities=True, normalization_model=True, model_python=model_python,
                               **({"clinical_profile": clinical_profile} if clinical_profile else {}))
    freeze(root / "fulltext-followup-result.json", result)
    return result


def deliver_batch(root, dictionary_path, registry_path=None, *, import_to_local=False, output_name="final",
                  normalize_entities=False, normalization_model=False, model_python=MODEL_PYTHON,
                  clinical_profile=None):
    """Materialize all knowledge, target-ready rows, and explicit remaining gaps."""
    from apps.curation.knowledge_export import export_bundle
    from apps.curation.knowledge_target_mapping import (
        load_gene_standard,
        prepare_mappings,
    )

    source = root / "delivery/bundle.json"
    bundle, dictionary = read(source), read(dictionary_path)
    if not output_name or Path(output_name).name != output_name or output_name in {".", ".."}:
        raise ValueError("Final output must be one directory name within the batch")
    output = root / output_name
    normalization = None
    if normalize_entities:
        from scripts.normalize_knowledge_entities import import_patch, run_normalization

        if registry_path is None:
            from scripts.export_local_entity_registry import snapshot

            registry_path = root / "entity-registry-input.json"
            if not registry_path.exists():
                # Freeze this batch's starting point; later batches read the
                # then-current local registry, including learned identities.
                database = Path(os.environ.get("BIOMED_REGISTRY_DATABASE",
                                "/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3"))
                freeze(registry_path, snapshot(database))
        normalization = run_normalization(source, registry_path, root / f"entity-normalization-{output_name}",
                                          use_model=normalization_model, model_python=model_python)
        registry_path = normalization["registry"]
        if import_to_local:
            receipt_path = normalization["patch"].parent / "import-receipt.json"
            if not receipt_path.exists():
                freeze(receipt_path, import_patch(normalization["patch"]))
        freeze(output / "entity-normalization.json", normalization["report"])
    registry = read(registry_path) if registry_path else None
    if clinical_profile not in (None, CLINICAL_PROFILE):
        raise ValueError("Unknown clinical profile")
    clinical_manifest = {}
    if clinical_profile:
        from apps.curation.knowledge_clinical_mapping import (
            adapt,
            load_gene_alias_standard,
        )

        genes = load_gene_alias_standard(ROOT / "data/knowledge/genes.jsonl")
        translation_file = root / "clinical-translations.json"
        if not translation_file.exists():
            freeze(translation_file, read(Path("/appdata/02_resource/apps/biomed/resources/cancer-20261025/translations.json")))
        translations = read(translation_file)
        clinical_manifest = {"clinical_profile": clinical_profile,
                             "gene_standard_sha256": genes["source"]["sha256"],
                             "translations_sha256": sha(translation_file)}

        def map_export(value):
            return adapt(value, dictionary, registry, genes, translations)
    else:
        genes = load_gene_standard(ROOT / "data/knowledge/genes.jsonl")

        def map_export(value):
            mappings, report = prepare_mappings(value, dictionary, registry, gene_standard=genes)
            return mappings, export_bundle(value, dictionary=dictionary, mappings=mappings), report

    freeze(output / "source-manifest.json", {
        "version": VERSION, "bundle_sha256": sha(source),
        "dictionary_sha256": sha(dictionary_path),
        "registry_sha256": sha(registry_path) if registry_path else None,
        "target_contacted": False, "target_written": False,
        **clinical_manifest,
    })
    mappings, preliminary, _ = map_export(bundle)
    final = copy.deepcopy(bundle)
    for record, target in zip(final["records"], preliminary["records"], strict=True):
        record.setdefault("assessment", {})["compatibility"] = {
            "status": target["compatibility_status"], "issues": target["mapping_issues"],
            "contract_validation": target["contract_validation"], "submit_ready": False,
        }
    final.setdefault("provenance", {}).update(
        original_extraction_bundle_sha256=sha(source),
        target_dictionary_sha256=sha(dictionary_path),
        registry_snapshot_sha256=sha(registry_path) if registry_path else None,
    )
    mappings, package, report = map_export(final)
    freeze(output / "knowledge.json", final)
    freeze(output / "target-mappings.json", mappings)
    freeze(output / "target-mapping-report.json", report)
    freeze(output / "tumor-api-export.json", package)
    freeze(output / "target-entity-operations.json", {
        "source_run": final["source_run"], "operations": package["entity_operations"],
        "operation_count": len(package["entity_operations"]),
        "ready_after_entity_sync_count": package["ready_after_entity_sync_count"],
        "target_contacted": False, "target_written": False,
    })
    ready = [record for record in package["records"] if record["offline_ready"]]
    freeze(output / "tumor-api-offline-ready.json", {
        "schema_version": package["schema_version"], "source_run": final["source_run"],
        "source_bundle_sha256": package["source_bundle_sha256"], "records": ready,
        "offline_ready_count": len(ready), "submit_ready": False,
        "note": "Offline source/field checks only; no live serializer, sample matching, or production import.",
    })
    freeze(output / "tumor-api-gaps.json", {
        "source_run": final["source_run"],
        "records": [record for record in package["records"] if not record["offline_ready"]],
    })
    if import_to_local:
        receipt_path = output / "import-receipt.json"
        if receipt_path.exists():
            receipt = read(receipt_path)
            if receipt.get("bundle_sha256") != sha(output / "knowledge.json"):
                raise ValueError("Prior local import receipt refers to different knowledge")
        else:
            with (output / "knowledge.json").open("rb") as stream:
                process = subprocess.run(
                    ["docker", "compose", "exec", "-T", "web", "python", "manage.py",
                     "import_knowledge_candidates", "--input", "/dev/stdin"],
                    cwd=ROOT, stdin=stream, capture_output=True, check=False)
            if process.returncode:
                raise RuntimeError(process.stderr.decode(errors="replace")[-2000:])
            value = json.loads(process.stdout)
            if value.get("dry_run") or "entity_registry" not in value:
                raise ValueError("Local import did not confirm entity registry integration")
            freeze(receipt_path, {"bundle_sha256": sha(output / "knowledge.json"),
                                  "result": value, "published": False, "target_written": False})
    extraction = read(root / "delivery/summary.json")
    articles = {row["pmid"]: copy.deepcopy(row) for row in delivery_classification(root)["records"]}
    for row in extraction["article_outcomes"]:
        articles[row["pmid"]]["extraction"] = row
    result = {"version": VERSION, "state": extraction["state"], "articles": list(articles.values()),
              "knowledge_records": len(final["records"]), "compatibility_counts": package["counts"],
              "offline_ready_count": len(ready), "quality": "unreviewed; independent of extraction",
              "automatic_entity_operation_count": len(package["entity_operations"]),
              "ready_after_entity_sync_count": package["ready_after_entity_sync_count"],
              "entity_normalization": normalization["report"]["counts"] if normalization else None,
              "target_written": False, "submit_ready": False}
    freeze(output / "summary.json", result)
    return result


def positive_integer(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be a positive integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, action="append", type=Path)
    parser.add_argument("--selection-file", required=True, type=Path)
    parser.add_argument("--model-python", type=Path, default=MODEL_PYTHON)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--classify-only", action="store_true")
    parser.add_argument("--deliver-only", action="store_true")
    parser.add_argument("--resume-fulltext-fallback", action="store_true",
                        help="Append recovered safety articles after an already-frozen extraction batch")
    parser.add_argument("--dictionary", type=Path, help="Frozen tumor_api dictionary; no target connection")
    parser.add_argument("--registry", type=Path, help="Optional frozen local registry for reproducible replay; otherwise snapshot the current biomed registry")
    parser.add_argument("--delivery-version", default="final", help="New final directory when adapter methods change")
    parser.add_argument("--per-document-limit", type=positive_integer,
                        help="Process the first N eligible positive passages per article; omit for all")
    parser.add_argument("--import-to-local", action="store_true")
    parser.add_argument("--clinical-profile", choices=[CLINICAL_PROFILE])
    args = parser.parse_args()
    if args.deliver_only and not args.dictionary:
        parser.error("--deliver-only requires --dictionary")
    if args.import_to_local and not args.dictionary:
        parser.error("--import-to-local requires --dictionary for the complete batch delivery")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    snapshots = [path.resolve(strict=True) for path in args.snapshot]
    pmids = selection_pmids(read(args.selection_file))
    with (root / "batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = root / "batch-manifest.json"
        manifest = {
            "version": VERSION, "pmids": pmids,
            "snapshots": {str(path): sha(path) for path in snapshots},
            "selection_sha256": sha(args.selection_file),
            "classifier": str(CLASSIFIER), "model_python": str(args.model_python),
            "target_written": False,
        }
        if manifest_path.exists():
            frozen_profile = read(manifest_path).get("clinical_profile")
            if args.clinical_profile and args.clinical_profile != frozen_profile:
                raise ValueError("Cannot change the frozen clinical profile")
            args.clinical_profile = frozen_profile
        if args.clinical_profile:
            manifest["clinical_profile"] = args.clinical_profile
        clinical_kwargs = {"clinical_profile": args.clinical_profile} if args.clinical_profile else {}
        relation_kwargs = {"relation_profile": args.clinical_profile} if args.clinical_profile else {}
        if (args.deliver_only or args.resume_fulltext_fallback) and manifest_path.exists():
            prior = read(manifest_path)
            if "per_document_limit" in prior:
                manifest["per_document_limit"] = prior["per_document_limit"]
            if args.deliver_only and args.per_document_limit is not None and args.per_document_limit != prior.get("per_document_limit"):
                raise ValueError("Delivery cannot change the frozen per-document limit")
        elif args.per_document_limit is not None and not args.resume_fulltext_fallback:
            manifest["per_document_limit"] = args.per_document_limit
        freeze(manifest_path, manifest)
        if args.deliver_only:
            print(json.dumps(deliver_batch(root, args.dictionary, args.registry,
                                           import_to_local=args.import_to_local,
                                           output_name=args.delivery_version, normalize_entities=True,
                                           normalization_model=False, model_python=args.model_python,
                                           **clinical_kwargs), ensure_ascii=False))
            return
        if args.resume_fulltext_fallback:
            print(json.dumps(resume_fulltext_fallback(root, snapshots, args.model_python,
                                                      args.dictionary, args.registry,
                                                      import_to_local=args.import_to_local,
                                                      per_document_limit=args.per_document_limit,
                                                      **clinical_kwargs), ensure_ascii=False))
            return
        prepare_classification(root, snapshots, pmids)
        if args.prepare_only:
            print(json.dumps({"status": "prepared", "articles": len(pmids)}))
            return
        selection = run_classification(root, args.model_python)
        print(json.dumps({"stage": "classification", "categories": selection["categories"]}), flush=True)
        if args.classify_only:
            return
        frozen_selection = root / "selection.json"
        if not frozen_selection.exists():
            selection, _ = route_with_fulltext(root, snapshots, selection, args.model_python)
            if not selection["solid_pmids"]:
                freeze(root / "batch-result.json", {"state": "no_solid_articles", "articles": selection["records"],
                                                    "knowledge_records": 0, "target_written": False})
                return
            freeze_snapshot_sources(root, snapshots, selection["solid_pmids"])
        from scripts.run_minimal_pipeline import screen_pipeline

        # Keep a unique source_run equal to the batch name, never a shared
        # generic subdirectory name such as "extraction" across different runs.
        extraction = root
        screen_pipeline(extraction, model_python=args.model_python, **relation_kwargs)
        result = run_pipeline(extraction, model_python=args.model_python, import_to_local=False,
                              per_document_limit=args.per_document_limit, **relation_kwargs)
        freeze(root / "extraction-result.json", result)
        if args.dictionary:
            result = deliver_batch(root, args.dictionary, args.registry, import_to_local=args.import_to_local,
                                   output_name=args.delivery_version, normalize_entities=True,
                                   normalization_model=True, model_python=args.model_python, **clinical_kwargs)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
