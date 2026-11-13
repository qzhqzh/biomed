"""Reclassify selected safety articles using frozen parsed full text, offline.

The original metadata classification and source snapshots are never changed.
This narrow fallback routes candidates only; it neither screens passages nor
imports, publishes, or infers facts. Oversized model inputs remain unresolved.
"""

import argparse
import fcntl
import json
import re
import sys
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import checked_input, digest
from apps.curation.three_class import CATEGORIES, validate_output
from scripts.benchmark_knowledge_candidates import completed_results, load_inputs
from scripts.prepare_knowledge_intake import freeze, read, sha
from scripts.prepare_minimal_e2e import selection_pmids
from scripts.prepare_passage_pilot import verify_document
from scripts.run_minimal_pipeline import MODEL_PYTHON, execute_command

VERSION = "literature-fulltext-safety-fallback-v1"
CLASSIFIER = Path("/data/models/Qwen3.5-4B")
CLASSIFIER_ID = "Qwen/Qwen3.5-4B"
CLASS_DEFINITION = ROOT / "var/experiments/three-class-20261022/task-definition-v1.json"

# Only candidate retrieval. A match does not assign a cancer type or outcome.
ONCOLOGY = re.compile(
    r"\b(?:cancers?|tumou?rs?|oncolog\w*|neoplas\w*|carcinomas?|melanomas?|"
    r"sarcomas?|gliomas?|glioblastomas?|lymphomas?|leuk(?:a|ae)emias?|myelomas?)\b",
    re.IGNORECASE,
)
ICI = re.compile(
    r"\b(?:icis?|immune[ -]checkpoint|checkpoint[ -](?:inhibit\w*|blockade)|"
    r"pd[ -]?1|pd[ -]?l1|ctla[ -]?4|nivolumab|pembrolizumab|ipilimumab|"
    r"atezolizumab|avelumab|durvalumab|cemiplimab|dostarlimab|relatlimab|"
    r"tremelimumab|tislelizumab|toripalimab)\b",
    re.IGNORECASE,
)
SAFETY = re.compile(
    r"\b(?:adverse|toxic\w*|toxicit\w*|side[ -]effects?|safety|iraes?|"
    r"immune[ -]related|complications?|drug[ -]induced|treatment[ -]related)\b",
    re.IGNORECASE,
)


def metadata_signals(metadata):
    """Use only metadata to nominate the fallback; no label is inferred."""
    text = " ".join([metadata["title"], metadata["abstract"],
                     *metadata["mesh"], *metadata["publication_types"]])
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    oncology, ici, safety = (pattern.search(text) for pattern in (ONCOLOGY, ICI, SAFETY))
    return {"oncology": bool(oncology), "ici": bool(ici), "safety": bool(safety),
            "candidate": bool((oncology or ici) and safety)}


def fulltext_definition(original):
    if original.get("categories") != list(CATEGORIES):
        raise ValueError("Original three-class labels changed")
    before = "Use only the supplied title, abstract, MeSH and publication types."
    after = ("Use the supplied title, abstract, MeSH, publication types and all supplied parsed "
             "full-text paragraphs. Distinguish the actual study subject and current treatment "
             "from background or unrelated past medical history.")
    prompt = original.get("prompt", "")
    if prompt.count(before) != 1:
        raise ValueError("Original classification scope cannot be updated safely")
    return {"version": VERSION, "system_prompt": prompt.replace(before, after)}


def prepare(snapshots, classification_selection, output, *, pmids=None, definition_path=CLASS_DEFINITION):
    output, classification_selection = Path(output), Path(classification_selection)
    if output.resolve() == classification_selection.resolve().parent:
        raise ValueError("Fallback output must be separate from the original classification directory")
    original_selection = read(classification_selection)
    prior = original_selection.get("records")
    if not isinstance(prior, list) or not prior:
        raise ValueError("Original classification selection has no records")
    original_pmids = selection_pmids({"records": prior})
    chosen = set(selection_pmids(pmids)) if pmids is not None else set(original_pmids)
    if chosen - set(original_pmids):
        raise ValueError(f"Requested PMIDs are not in the original classification: {sorted(chosen - set(original_pmids))}")

    sources, candidates = {}, {}
    for supplied in snapshots:
        path = Path(supplied).resolve(strict=True)
        envelope = read(path)
        snapshot = envelope["snapshot"]
        if digest(snapshot) != envelope["sha256"]:
            raise ValueError(f"Source snapshot digest mismatch: {path}")
        key = str(path)
        if key in sources:
            raise ValueError("Duplicate source snapshot")
        sources[key] = {"file_sha256": sha(path), "payload_sha256": envelope["sha256"]}
        library = Path(snapshot.get("readable_library") or snapshot["library"])
        for document in snapshot["documents"]:
            pmid = str(document.get("pmid") or "")
            if pmid in original_pmids:
                candidates.setdefault(pmid, []).append((document, library, key))
    if not sources:
        raise ValueError("At least one source snapshot is required")

    entries, rows, details = [], [], {}
    for prior_row in prior:
        pmid = str(prior_row["pmid"])
        category, classification_status = prior_row.get("category"), prior_row.get("status")
        if category not in (*CATEGORIES, None):
            raise ValueError(f"Invalid original category for {pmid}")
        entry = {"pmid": pmid, "original_status": classification_status, "original_category": category}
        entries.append(entry)
        if category in {"solid", "hematologic"}:
            entry["fallback_status"] = "already_" + category
            continue
        matches = candidates.get(pmid, [])
        if not matches:
            entry["fallback_status"] = "not_found_in_snapshots"
            continue
        if len({digest(match[0]) for match in matches}) > 1:
            entry["fallback_status"] = "ambiguous_snapshot_documents"
            continue
        document, library, source_path = matches[0]
        entry["source_snapshot"] = source_path
        source = document.get("model_input")
        if not source:
            entry["fallback_status"] = "metadata_unavailable"
            continue
        try:
            source = checked_input(source)
            if source["pmid"] != pmid:
                raise ValueError("Metadata PMID differs from selected source")
            entry["signals"] = metadata_signals(source["metadata"])
        except (KeyError, TypeError, ValueError) as exc:
            entry.update(fallback_status="metadata_unusable", error=f"{type(exc).__name__}: {exc}")
            continue
        if not entry["signals"]["candidate"]:
            entry["fallback_status"] = "not_a_safety_candidate"
            continue
        if pmid not in chosen:
            entry["fallback_status"] = "candidate_not_selected"
            continue
        if not document.get("fulltext_version_id") or document.get("input_scope") in {"abstract_only", "metadata_only"}:
            entry.update(fallback_status="fulltext_missing", source_issues=document.get("source_issues", []))
            continue
        try:
            version, provenance = verify_document(document, library)
            if (provenance["metadata_version"]["id"] != source["version_id"]
                    or provenance["metadata_version"]["sha256"] != source["source_sha256"]):
                raise ValueError("Metadata model input differs from the verified source version")
            passages = [{key: p[key] for key in ("id", "locator", "ordinal", "text")} for p in document["passages"]]
            if not passages:
                raise ValueError("No parsed full-text paragraphs")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
                StopIteration, ParseError, DefusedXmlException) as exc:
            entry.update(fallback_status="source_unusable", error=f"{type(exc).__name__}: {exc}")
            continue
        prompt = json.dumps({"metadata": source["metadata"], "parsed_fulltext_paragraphs": passages}, ensure_ascii=False)
        row = {"id": pmid, "pmid": pmid, "metadata_version_id": source["version_id"],
               "metadata_source_sha256": source["source_sha256"], "metadata_input_sha256": source["input_sha256"],
               "fulltext_version_id": version["id"], "fulltext_source_sha256": version["sha256"],
               "source_snapshot_sha256": sources[source_path]["payload_sha256"], "prompt": prompt}
        row["input_sha256"] = digest(row)
        rows.append(row)
        details[pmid] = provenance
        entry.update(fallback_status="prepared", fulltext_version_id=version["id"],
                     fulltext_source_sha256=version["sha256"], metadata_version_id=source["version_id"],
                     metadata_source_sha256=source["source_sha256"], passages=len(passages),
                     passage_characters=sum(len(p["text"]) for p in passages), input_sha256=row["input_sha256"])

    original_model = None
    original_manifest_path = classification_selection.parent / "extract/manifest.json"
    if original_manifest_path.exists():
        manifest = read(original_manifest_path)
        if digest(manifest["config"]) != manifest["config_sha256"]:
            raise ValueError("Original classifier manifest digest mismatch")
        original_model = {"model_id": manifest["config"].get("model_id"),
                          "revision": manifest["config"].get("revision"),
                          "manifest_sha256": sha(original_manifest_path)}
    original_definition = read(definition_path)
    definition = fulltext_definition(original_definition)
    dataset = {"records": rows}
    plan = {"version": VERSION, "original_classification_file_sha256": sha(classification_selection),
            "original_classification_sha256": digest(original_selection), "original_definition_sha256": sha(definition_path),
            "sources": sources, "original_classifier": original_model,
            "requested_pmids": original_pmids, "selected_pmids": sorted(chosen),
            "input_scope": "metadata and every stored JATS abstract/body paragraph; no OCR, table reconstruction, image or supplement content",
            "policy": "Metadata signals nominate only. Three-class model decides on complete supplied parsed paragraphs; no truncation.",
            "max_input_tokens": 6144, "input_truncation": False, "records": entries,
            "prepared_inputs": len(rows), "inputs_sha256": digest(dataset), "definition_sha256": digest(definition)}
    freeze(output / "original-classification-selection.json", original_selection)
    freeze(output / "original-definition.json", original_definition)
    freeze(output / "inputs.json", dataset)
    freeze(output / "definition.json", definition)
    for pmid, detail in details.items():
        freeze(output / f"documents/{pmid}.json", detail)
    freeze(output / "preparation.json", plan)
    if rows:
        load_inputs(output / "inputs.json")
    return plan


def run(output, *, model_python=MODEL_PYTHON, model=CLASSIFIER):
    output, model = Path(output).resolve(), Path(model)
    plan, dataset, definition = (read(output / name) for name in ("preparation.json", "inputs.json", "definition.json"))
    if digest(dataset) != plan["inputs_sha256"] or digest(definition) != plan["definition_sha256"]:
        raise ValueError("Prepared inputs or definition changed")
    results, model_manifest = {}, None
    if dataset["records"]:
        dataset = load_inputs(output / "inputs.json")
        model_info = read(model / "download-manifest.json")
        if model_info["repo"] != CLASSIFIER_ID:
            raise ValueError("Fallback requires the existing Qwen3.5-4B classifier")
        prior_model = plan.get("original_classifier")
        if prior_model and (prior_model["model_id"] != model_info["repo"] or prior_model["revision"] != model_info["revision"]):
            raise ValueError("Fallback model differs from the original classifier revision")
        extract = output / "extract"
        execute_command([str(model_python), "scripts/benchmark_knowledge_candidates.py", "--inputs", str(output / "inputs.json"),
                         "--definition", str(output / "definition.json"), "--output", str(extract), "--resume",
                         "--model", str(model), "--model-id", model_info["repo"], "--revision", model_info["revision"],
                         "--max-input-tokens", "6144", "--max-new-tokens", "128"], output / "logs/classification.log")
        model_manifest, summary = read(extract / "manifest.json"), read(extract / "summary.json")
        if (model_manifest["input_sha256"] != digest(dataset) or read(extract / "inputs.json") != dataset
                or digest(model_manifest["config"]) != model_manifest["config_sha256"]
                or model_manifest["definition_sha256"] != digest(definition)
                or model_manifest["config"]["definition"] != definition
                or model_manifest["config"].get("model_id") != model_info["repo"]
                or model_manifest["config"].get("revision") != model_info["revision"]
                or model_manifest["config"].get("max_input_tokens") != 6144
                or model_manifest["config"].get("input_truncation") is not False):
            raise ValueError("Fallback model output differs from frozen inputs or definition")
        results = completed_results(extract, dataset["records"], model_manifest["config_sha256"])
        if len(results) != len(dataset["records"]) or summary.get("state") != "completed":
            raise RuntimeError("Fallback inference incomplete; rerun the same command to resume")

    entries, selected = [], []
    for prepared in plan["records"]:
        entry = dict(prepared)
        if prepared["fallback_status"] == "prepared":
            result = results[prepared["pmid"]]
            entry.update(fallback_status=result["status"], fallback_category=None,
                         model_input_tokens=result["input_tokens"], model_result_sha256=digest(result))
            if result["status"] == "classified":
                try:
                    category = validate_output(result["raw_output"])
                    if result["payload"] != {"category": category}:
                        raise ValueError("Category output differs from raw output")
                    entry.update(fallback_status="classified" if category else "abstained", fallback_category=category)
                    if category == "solid":
                        selected.append(prepared["pmid"])
                except (ValueError, TypeError):
                    entry["fallback_status"] = "invalid_category_output"
        entries.append(entry)
    unresolved = {"abstained", "invalid_json", "invalid_category_output", "output_truncated", "input_too_long",
                  "metadata_unavailable", "metadata_unusable", "not_found_in_snapshots", "ambiguous_snapshot_documents",
                  "candidate_not_selected", "fulltext_missing", "source_unusable"}
    routing = {"version": VERSION, "state": "completed_with_unresolved" if any(
                   row["fallback_status"] in unresolved for row in entries) else "completed", "records": entries,
               "continue_pmids": selected, "fallback_statuses": dict(Counter(r["fallback_status"] for r in entries)),
               "preparation_sha256": sha(output / "preparation.json"),
               "model_manifest_sha256": sha(output / "extract/manifest.json") if model_manifest else None,
               "policy": "Only a fallback classified solid is routed. Original metadata classifications remain unchanged.",
               "classification_scope": plan["input_scope"], "input_truncation": False, "database_written": False}
    freeze(output / "routing.json", routing)
    freeze(output / "continue-pmids.json", {"pmids": selected})
    return routing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--snapshot", type=Path, required=True, action="append")
    p.add_argument("--classification-selection", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--pmid", action="append", help="Restrict fallback to these candidate PMIDs; repeat as needed")
    p = commands.add_parser("run")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model-python", type=Path, default=MODEL_PYTHON)
    p.add_argument("--model", type=Path, default=CLASSIFIER)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.command == "prepare" and output == args.classification_selection.resolve().parent:
        parser.error("Fallback output must be separate from the original classification directory")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "fallback.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            result = prepare(args.snapshot, args.classification_selection, output, pmids=args.pmid)
        else:
            result = run(output, model_python=args.model_python, model=args.model)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
