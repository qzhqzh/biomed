"""Freeze unused full texts, then assemble packets from actual passage decisions.

Only existing immutable research snapshots and JATS blobs are read. No database,
network, model inference, production changes, or hand-selected positive passages.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.knowledge_eligibility import anchor_eligibility
from scripts.benchmark_passage_screening import (
    completed_results,
    load_inputs,
)
from scripts.prepare_knowledge_intake import freeze, read
from scripts.prepare_passage_pilot import verify_document

VERSION = "minimal-e2e-preparation-v4"
DEFAULT_OUTPUT = ROOT / "var/experiments/minimal-e2e-20261101"
SOURCE_RUNS = ("evidence-pilot-20261022-first20", "evidence-pilot-20261022-solid10")
SELECTION = {
    "42272324": "Colorectal cancer: bevacizumab treatment and predictive biomarkers; original cohort.",
    "42050770": "Muscle-invasive bladder cancer: neoadjuvant regimens; systematic review/network meta-analysis (secondary evidence retained).",
    "42068240": "Differentiated thyroid cancer: lenvatinib plus antibiotics; experimental combination treatment.",
    "42858231": "Triple-negative breast cancer: pathological response under neoadjuvant treatment; prospective cohort.",
    "42845214": "Colorectal cancer: WNT inhibition plus retinoids; preclinical treatment combinations.",
}


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def screening_rows(document, version):
    """Use exactly the existing screening protocol: center plus adjacent text."""
    result = []
    passages = document["passages"]
    for index, passage in enumerate(passages):
        row = {"id": passage["id"], "pmid": document["pmid"], "title": document["title"],
               "version_id": passage["version_id"], "source_sha256": version["sha256"],
               "parser_version": passage["parser_version"], "locator": passage["locator"],
               "ordinal": passage["ordinal"], "text": passage["text"],
               "previous_text": passages[index - 1]["text"] if index else "",
               "next_text": passages[index + 1]["text"] if index + 1 < len(passages) else ""}
        row["input_sha256"] = digest(row)
        result.append(row)
    return result


def freeze_sources(output=DEFAULT_OUTPUT, experiments=ROOT / "var/experiments"):
    exposed = {row["pmid"] for row in read(experiments / "evidence-passages-20261023/inputs.json")["documents"]}
    if exposed & set(SELECTION):
        raise ValueError("Selected article was used in passage/extraction development")
    sources, selected = {}, {}
    for run in SOURCE_RUNS:
        path = experiments / run / "snapshot.json"
        envelope = read(path)
        snapshot = envelope["snapshot"]
        if digest(snapshot) != envelope["sha256"]:
            raise ValueError("Source snapshot digest mismatch")
        sources[run] = {"file_sha256": file_sha(path), "payload_sha256": envelope["sha256"]}
        library = Path(snapshot.get("readable_library") or snapshot["library"])
        for document in snapshot["documents"]:
            if document["pmid"] in SELECTION:
                if document["pmid"] in selected:
                    raise ValueError("Duplicate selected article across snapshots")
                version, provenance = verify_document(document, library)
                selected[document["pmid"]] = (document, version, provenance, run)
    if set(selected) != set(SELECTION):
        raise ValueError("All five preselected full texts must exist; no silent substitution")
    rows, documents, provenance = [], [], {}
    for pmid, selection_reason in SELECTION.items():
        document, version, source, run = selected[pmid]
        rows.extend(screening_rows(document, version))
        documents.append({"pmid": pmid, "title": document["title"], "version_id": version["id"],
                          "source_sha256": version["sha256"], "passage_count": len(document["passages"]),
                          "provenance_file": f"documents/{pmid}.json"})
        provenance[pmid] = {**source, "selection_reason": selection_reason, "source_snapshot": run}
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate passage identity across articles")
    dataset = {"schema_version": "evidence-passages-v1", "documents": documents, "records": rows}
    definition = read(experiments / "evidence-passages-20261023/definition.json")
    freeze(output / "screening/inputs.json", dataset)
    freeze(output / "screening/definition.json", definition)
    for pmid, value in provenance.items():
        freeze(output / f"screening/documents/{pmid}.json", value)
    plan = {"version": VERSION, "selected_pmids": list(SELECTION), "selection_reasons": SELECTION,
            "sources": sources, "excluded_extraction_development_pmids": sorted(exposed),
            "articles": len(documents), "passages": len(rows), "inputs_sha256": digest(dataset),
            "previous_classification_exposure": True, "extraction_development_exposure": False,
            "independent_statistical_holdout": False,
            "policy": "Fixed article selection from titles/metadata; all parsed paragraphs, no prediction-dependent sampling. JATS hashes, PMID and all stored locator texts verified.",
            "limits": "Existing parser p elements only; no OCR, table reconstruction, new fulltext download or guideline claims.",
            "database_read": False, "network_access": False, "model_calls": 0}
    freeze(output / "selection.json", plan)
    load_inputs(output / "screening/inputs.json", len(rows))
    return plan


def selection_pmids(value):
    """Accept a PMID array, {'pmids': [...]}, or records with PMID fields."""
    if isinstance(value, dict):
        value = value.get("pmids", value.get("records"))
    if not isinstance(value, list) or not value:
        raise ValueError("Selection must contain a nonempty PMID list")
    result = []
    for row in value:
        pmid = row.get("pmid") if isinstance(row, dict) else row
        if not isinstance(pmid, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(pmid)):
            raise ValueError("Selection contains an invalid or missing PMID")
        result.append(str(pmid))
    if len(set(result)) != len(result):
        raise ValueError("Selection contains duplicate PMIDs")
    return result


def freeze_snapshot_sources(output, snapshots, pmids=None, experiments=ROOT / "var/experiments"):
    """Freeze requested articles from existing snapshots without opening a database.

    The source selection and every failure remain immutable; retry missing or
    invalid sources using a new batch/snapshot rather than rewriting this one.
    """
    if not snapshots:
        raise ValueError("At least one frozen snapshot is required")
    sources, candidates = {}, {}
    for source_path in map(Path, snapshots):
        path = source_path.resolve(strict=True)
        envelope = read(path)
        snapshot = envelope["snapshot"]
        if digest(snapshot) != envelope["sha256"]:
            raise ValueError(f"Source snapshot digest mismatch: {path}")
        key = str(path)
        if key in sources:
            raise ValueError("Duplicate source snapshot")
        sources[key] = {"file_sha256": file_sha(path), "payload_sha256": envelope["sha256"]}
        library = Path(snapshot.get("readable_library") or snapshot["library"])
        for document in snapshot["documents"]:
            pmid = str(document.get("pmid") or "")
            if re.fullmatch(r"[1-9][0-9]{0,9}", pmid):
                candidates.setdefault(pmid, []).append((document, library, key))
    requested = selection_pmids(pmids) if pmids is not None else list(candidates)
    if not requested:
        raise ValueError("Snapshots contain no valid PMID; provide a PMID selection")
    rows, documents, provenance, statuses = [], [], {}, []
    used_passage_ids = set()
    for pmid in requested:
        matches = candidates.get(pmid, [])
        if not matches:
            statuses.append({"pmid": pmid, "status": "not_found_in_snapshots"})
            continue
        # An identical frozen document may occur in more than one snapshot.
        identities = {digest(item[0]) for item in matches}
        if len(identities) > 1:
            statuses.append({"pmid": pmid, "status": "ambiguous_snapshot_documents",
                             "sources": [item[2] for item in matches]})
            continue
        document, library, source = matches[0]
        if not document.get("fulltext_version_id") or document.get("input_scope") in {"abstract_only", "metadata_only"}:
            statuses.append({"pmid": pmid, "status": "fulltext_missing",
                             "source_issues": document.get("source_issues", []), "source_snapshot": source})
            continue
        try:
            version, detail = verify_document(document, library)
            article_record(detail)  # Validate fields required by downstream grouping per article.
            article_rows = screening_rows(document, version)
            ids = [row["id"] for row in article_rows]
            if not ids:
                raise ValueError("No parsed full-text passages")
            if len(set(ids)) != len(ids) or used_passage_ids.intersection(ids):
                raise ValueError("Duplicate passage identity")
            if (not isinstance(document.get("title"), str) or not document["title"].strip()
                    or any(not isinstance(row["id"], str)
                           or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", row["id"])
                           or not isinstance(row["parser_version"], str) or not row["parser_version"]
                           for row in article_rows)):
                raise ValueError("Invalid passage identity, title, or parser provenance")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
                StopIteration, ParseError, DefusedXmlException) as exc:
            statuses.append({"pmid": pmid, "status": "source_unusable", "source_snapshot": source,
                             "error": f"{type(exc).__name__}: {exc}"})
            continue
        used_passage_ids.update(ids)
        rows.extend(article_rows)
        documents.append({"pmid": pmid, "title": document["title"], "version_id": version["id"],
                          "source_sha256": version["sha256"], "passage_count": len(article_rows),
                          "provenance_file": f"documents/{pmid}.json"})
        provenance[pmid] = {**detail, "source_snapshot": source}
        statuses.append({"pmid": pmid, "status": "ready_for_screening", "passages": len(article_rows),
                         "source_snapshot": source})
    dataset = {"schema_version": "evidence-passages-v1", "documents": documents, "records": rows}
    definition = read(experiments / "evidence-passages-20261023/definition.json")
    plan = {"version": "minimal-e2e-preparation-v3", "requested_pmids": requested,
            "selected_pmids": [doc["pmid"] for doc in documents], "source_statuses": statuses,
            "sources": sources, "requested_articles": len(requested), "articles": len(documents),
            "passages": len(rows), "inputs_sha256": digest(dataset),
            "policy": "All verified parsed full-text paragraphs; no prediction-dependent selection or silent replacement.",
            "limits": "Existing JATS p elements only; no OCR, table reconstruction, network or database access.",
            "source_status_counts": dict(Counter(item["status"] for item in statuses)),
            "database_read": False, "network_access": False, "model_calls": 0}
    freeze(output / "screening/inputs.json", dataset)
    freeze(output / "screening/definition.json", definition)
    for pmid, value in provenance.items():
        freeze(output / f"screening/documents/{pmid}.json", value)
    freeze(output / "selection.json", plan)
    if rows:
        load_inputs(output / "screening/inputs.json", len(rows))
    return plan


def article_record(document):
    version = document["fulltext_version"]
    identifiers = document["article_identifiers"]
    return {"pmid": document["pmid"], "pmcid": identifiers.get("pmcid") or identifiers.get("pmc"),
            "doi": identifiers.get("doi"), "title": document["title"],
            "material_id": document["material_id"], "version_id": version["id"],
            "source_sha256": version["sha256"], "source_url": version["source_url"],
            "rights": version["rights"], "acquired_at": version["acquired_at"]}


def assemble_packets(dataset, decisions, articles, *, max_context_characters=12000, structure_by_passage=None):
    """Every positive center receives all abstracts and immediate neighbors.

    No text is truncated. Over-budget packets remain present and are separately
    reported; the caller must resolve the budget before invoking an extractor.
    """
    if type(max_context_characters) is not int or max_context_characters < 1:
        raise ValueError("Context character budget must be positive")
    rows = dataset["records"]
    if set(decisions) != {row["id"] for row in rows}:
        raise ValueError("Every screened passage requires a result; resume screening first")
    by_article = {}
    for row in rows:
        by_article.setdefault(row["pmid"], []).append(row)
    for group in by_article.values():
        group.sort(key=lambda row: row["ordinal"])
    structure_by_passage = structure_by_passage or {}
    packets, deferred, negatives, oversized, excluded = [], [], [], [], []
    for pmid, group in by_article.items():
        abstracts = [row for row in group if str(row["locator"]).startswith("front/article-meta/abstract/")]
        # Eligibility is often screened negative as a finding, but is required
        # to interpret cohort outcomes. Keep whole original paragraphs, with
        # their source IDs; never silently shorten them to meet a token budget.
        eligibility_context = [row for row in group if re.match(
            r"(?:patients|participants)\s+were\s+eligible\s+if\b", row["text"], re.IGNORECASE)]
        for index, row in enumerate(group):
            result = decisions[row["id"]]
            if result["input_sha256"] != row["input_sha256"]:
                raise ValueError("Decision belongs to different source input")
            if result["status"] != "classified":
                deferred.append({"id": row["id"], "pmid": pmid, "status": result["status"], "reason": "screening_unresolved_not_negative"})
                continue
            if type(result.get("relevant")) is not bool:
                raise ValueError("Classified result requires boolean relevant")
            if not result["relevant"]:
                negatives.append(row["id"])
                continue
            structure = structure_by_passage.get(row["id"], {})
            eligibility = anchor_eligibility({**row, **structure})
            if eligibility["status"] == "deferred":
                excluded.append({"id": row["id"], "pmid": pmid, "eligibility": eligibility,
                                 "original_screened_passage": copy.deepcopy(row),
                                 "structure": copy.deepcopy(structure), "screening_decision_sha256": digest(result)})
                continue
            context = {item["id"]: item for item in abstracts + eligibility_context + group[max(0, index - 1):index + 2]}
            passages = []
            for n, item in enumerate(sorted(context.values(), key=lambda p: p["ordinal"]), 1):
                passage = {k: item[k] for k in ("id", "pmid", "version_id", "source_sha256", "parser_version",
                                               "locator", "ordinal", "text", "input_sha256")}
                item_structure = structure_by_passage.get(item["id"], {})
                if "ancestor_tags" in item_structure:
                    passage["ancestor_tags"] = copy.deepcopy(item_structure["ancestor_tags"])
                source_decision = decisions[item["id"]]
                passage.update(alias=f"P{n}", role="anchor" if item["id"] == row["id"] else "support",
                               upstream_status=source_decision["status"], upstream_relevant=source_decision.get("relevant"),
                               upstream_decision_sha256=digest(source_decision))
                passages.append(passage)
            size = sum(len(p["text"]) for p in passages)
            packet = {"id": f"E-{pmid}-{row['ordinal']:04d}", "article_pmid": pmid,
                      "article": copy.deepcopy(articles[pmid]), "anchor_id": row["id"], "passages": passages,
                      "context_characters": size, "context_budget_characters": max_context_characters,
                      "context_budget_status": "within_budget" if size <= max_context_characters else "over_budget"}
            packet["input_sha256"] = digest(packet)
            packets.append(packet)
            if size > max_context_characters:
                oversized.append({"id": packet["id"], "characters": size, "limit": max_context_characters})
    report = {"version": VERSION, "screened_passages": len(rows), "positive_packets": len(packets),
              "screening_positive_passages": len(packets) + len(excluded), "excluded_evidence_centers": excluded,
              "negative_passages": len(negatives), "deferred_screening": deferred, "oversized_packets": oversized,
              "context_policy": "Same article all JATS abstract p elements, explicit 'patients/participants were eligible if' paragraphs, and full immediate predecessor/center/successor. Title is metadata only; support may be S2-negative and is never counted as a selected anchor.",
              "budget_policy": f"No silent truncation or context dropping. {max_context_characters}-character preparation budget; runner must additionally enforce actual tokenizer/context budget including instructions.",
              "unresolved_are_not_negative": True, "manual_positive_selection": False,
              "center_eligibility_policy": "JATS def-list paragraphs remain in original sources and may supply context, but never serve as independent evidence centers. No text-length or numeric-results filter."}
    return {"schema_version": "knowledge-candidate-inputs-v1", "documents": list(articles.values()), "records": packets}, report


def prepare_packets(root, results, output, max_context_characters=12000):
    selection = read(root / "selection.json")
    dataset = load_inputs(root / "screening/inputs.json", selection["passages"])
    manifest = read(results / "manifest.json")
    if manifest["input_sha256"] != digest(dataset) or read(results / "inputs.json") != dataset:
        raise ValueError("Screening manifest does not match the frozen source dataset")
    decisions = completed_results(results, dataset["records"], manifest["config_sha256"])
    documents = {pmid: read(root / f"screening/documents/{pmid}.json") for pmid in selection["selected_pmids"]}
    articles = {pmid: article_record(document) for pmid, document in documents.items()}
    structures = {item["id"]: item for document in documents.values() for item in document.get("paragraph_structure", [])}
    for row in dataset["records"]:
        if row["id"] in structures and structures[row["id"]].get("locator") != row["locator"]:
            raise ValueError("Paragraph structure does not match the screened source locator")
    packets, report = assemble_packets(dataset, decisions, articles, max_context_characters=max_context_characters,
                                       structure_by_passage=structures)
    report["screening_manifest_sha256"] = file_sha(results / "manifest.json")
    report["screening_config_sha256"] = manifest["config_sha256"]
    report["packets_sha256"] = digest(packets)
    report["articles"] = []
    for pmid in selection["selected_pmids"]:
        article_rows = [row for row in dataset["records"] if row["pmid"] == pmid]
        selected = [row for row in packets["records"] if row["article_pmid"] == pmid]
        report["articles"].append({
            "pmid": pmid, "screened_passages": len(article_rows),
            "positive_packets": len(selected),
            "negative_passages": sum(decisions[row["id"]].get("relevant") is False for row in article_rows),
            "unresolved_passage_ids": [row["id"] for row in article_rows if decisions[row["id"]]["status"] != "classified"],
            "excluded_center_ids": [row["id"] for row in report["excluded_evidence_centers"] if row["pmid"] == pmid],
            "over_budget_packet_ids": [row["id"] for row in selected if row["context_budget_status"] == "over_budget"],
        })
    freeze(output / "packets.json", packets)
    freeze(output / "packet-preparation.json", report)
    return report


def deduplicate_exact_facts(bundle):
    """Merge only exactly equal fact values within one source version.

    Local entity IDs and citation lists are lineage, not fact content; compare
    them after replacing IDs with exact kind/label/roles. No case folding,
    paraphrase matching, cross-article merge, or semantic equivalence inference.
    """
    result = copy.deepcopy(bundle)
    unique, seen, merged = [], {}, []
    for record in result["records"]:
        fact = copy.deepcopy(record["fact"])
        entities = {e["ref"]: e for e in record.get("entities", [])}
        roles = fact.pop("entity_roles", {})
        fact.pop("entity_ids", None)
        fact.pop("evidence", None)
        participants = sorted((entities[ref]["kind"], entities[ref]["label"], tuple(sorted(values)))
                              for ref, values in roles.items() if ref in entities)
        # An incomplete role/identity projection cannot establish exact equality.
        key = digest([record["version_id"], fact, participants,
                      record["group_id"] if not roles or set(roles) - set(entities) else None])
        if key not in seen:
            seen[key] = record
            unique.append(record)
            continue
        first = seen[key]
        duplicate = {"group_id": record["group_id"], "retained_group_id": first["group_id"],
                     "reason": "exact_equal_same_version_fact_and_participants"}
        # Keep the entire duplicate as lineage instead of remapping its local refs.
        first.setdefault("provenance", {}).setdefault("exact_duplicate_records", []).append(record)
        merged.append(duplicate)
    result["records"] = unique
    result.setdefault("provenance", {})["deduplication"] = {"method": "exact-same-version-fact-v1", "merged": merged}
    return result, {"input_records": len(bundle["records"]), "retained_records": len(unique), "merged": merged}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sources")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--experiments", type=Path, default=ROOT / "var/experiments")
    p.add_argument("--snapshot", type=Path, action="append", help="Frozen evidence_pilot snapshot.json; repeat as needed")
    select = p.add_mutually_exclusive_group()
    select.add_argument("--selection-file", type=Path, help="JSON PMID array, object with pmids, or records with pmid")
    select.add_argument("--pmid", action="append", help="Requested PMID; repeat as needed")
    p = sub.add_parser("packets")
    p.add_argument("--root", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-context-characters", type=int, default=12000)
    p = sub.add_parser("deduplicate")
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "sources":
        if args.snapshot or args.pmid or args.selection_file:
            snapshots = args.snapshot or [args.experiments / run / "snapshot.json" for run in SOURCE_RUNS]
            selected = selection_pmids(read(args.selection_file)) if args.selection_file else args.pmid
            report = freeze_snapshot_sources(args.output, snapshots, selected, args.experiments)
        else:
            report = freeze_sources(args.output, args.experiments)
    elif args.command == "packets":
        report = prepare_packets(args.root, args.results, args.output, args.max_context_characters)
    else:
        bundle, report = deduplicate_exact_facts(read(args.bundle))
        freeze(args.output, bundle)
    print(json.dumps(report, ensure_ascii=False))
