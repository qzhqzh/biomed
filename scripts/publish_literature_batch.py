"""Build a verifiable research-page projection of completed literature batches.

Only frozen local artifacts are read.  The optional registration appends a
research-history record; it does not publish knowledge, call models, or contact
the target system.  A missing local-import receipt permits an offline report,
but cannot create a research record with links to imaginary candidates.
"""

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from datetime import date as calendar_date
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.knowledge_export import digest, export_bundle
from apps.curation.knowledge_target_mapping import (
    load_gene_standard,
    prepare_mappings,
)
from apps.curation.research_history import (
    MAX_BYTES,
    prepare_record,
    register_record,
)
from scripts.prepare_knowledge_intake import freeze

VERSION = "literature-batch-research-v1"
DEFAULT_GENES = PROJECT / "data/knowledge/genes.jsonl"
FILES = ("knowledge.json", "tumor-api-export.json", "target-mappings.json",
         "target-mapping-report.json", "summary.json")
MODEL_SUMMARIES = (
    ("classification/extract/summary.json", "元数据分类"),
    ("screening/qwen35-9b/summary.json", "全文段落筛选"),
    ("stages/entities/extract/summary.json", "实体抽取"),
    ("stages/relations/extract/summary.json", "关系抽取"),
    ("fallback-fulltext-v1/extract/summary.json", "全文补判"),
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def candidate_id(source_run, group_id):
    """Check the importer's stable identity without opening a database."""
    from django.apps import apps
    from django.conf import settings

    if not settings.configured:
        from scripts.evidence_pilot import setup_helpers
        setup_helpers()
    elif not apps.ready:
        import django
        django.setup()
    from apps.curation.knowledge_storage import NAMESPACE

    return str(uuid.uuid5(NAMESPACE, "candidate:" + source_run + ":" + group_id))


def _index(rows, key, description):
    require(isinstance(rows, list), f"{description} must be an array")
    result = {}
    for row in rows:
        require(isinstance(row, dict), f"Invalid {description} record")
        value = row.get(key)
        require(isinstance(value, str) and bool(value.strip()) and value not in result,
                f"Invalid or duplicate {description} {key}")
        result[value] = row
    return result


def _registry_for(report, final, explicit_paths):
    expected = report.get("registry_sha256")
    if expected is None:
        return None, None
    candidates = [final.parent / "registry-snapshot.json", *explicit_paths]
    seen = set()
    for path in candidates:
        path = Path(path)
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        value = read(path)
        if digest(value) == expected:
            return value, path
    raise ValueError(f"Missing frozen registry for {final}; supply --registry")


def _genes_for(report, path, cache):
    expected = report.get("gene_standard_source")
    if expected is None:
        return None
    require(isinstance(expected, dict) and isinstance(expected.get("sha256"), str),
            "Invalid gene standard source")
    path = Path(path)
    require(path.is_file(), "Frozen gene standard is unavailable")
    key = (str(path.resolve()), expected["sha256"])
    if key not in cache:
        genes = load_gene_standard(path)
        require(genes["source"]["sha256"] == expected["sha256"],
                "Gene standard snapshot SHA differs")
        require(genes["source"]["matching"] == expected.get("matching"),
                "Gene standard matching policy differs")
        cache[key] = genes
    genes = copy.deepcopy(cache[key])
    # Preserve the producer's provenance path after checking the supplied bytes.
    genes["source"] = copy.deepcopy(expected)
    return genes


def _receipt(final, bundle):
    path = final / "import-receipt.json"
    if not path.exists():
        return None, None
    receipt = read(path)
    require(receipt.get("bundle_sha256") == file_sha(final / "knowledge.json"),
            "Import receipt bundle file SHA differs")
    require(receipt.get("target_written") is False and receipt.get("published") is False,
            "Import receipt must describe local, unpublished knowledge")
    result = receipt.get("result")
    require(isinstance(result, dict) and result.get("dry_run") is False,
            "Import receipt must represent a real local import")
    require(result.get("source_run") == bundle.get("source_run")
            and result.get("import_sha256") == digest(bundle),
            "Import receipt source run or payload SHA differs")
    require(isinstance(result.get("entity_registry"), dict),
            "Import receipt lacks the completed local entity registry result")
    records = _index(bundle["records"], "group_id", "knowledge")
    candidates = _index(result.get("candidates"), "group_id", "import candidate")
    require(set(candidates) == set(records) and result.get("candidate_count") == len(records),
            "Import candidate groups or count differ from knowledge")
    ids = set()
    for group, item in candidates.items():
        value = item.get("id")
        require(isinstance(value, str) and value == candidate_id(bundle["source_run"], group),
                "Import candidate ID differs from the persisted identity contract")
        require(value not in ids, "Duplicate import candidate ID")
        ids.add(value)
    return candidates, path


def _effective_routing(root, original, routed, effective):
    """Verify the derived ledger against all three frozen classification inputs."""
    original_path = root / "classification/selection.json"
    routed_path = root / "classification-routing.json"
    fallback_path = root / "fallback-fulltext-v1/routing.json"
    require(routed_path.is_file() and fallback_path.is_file(), "Effective routing provenance is missing")
    fallback = read(fallback_path)
    require(effective.get("original_selection_sha256") == file_sha(original_path)
            and effective.get("base_routing_sha256") == file_sha(routed_path)
            and effective.get("fallback_routing_sha256") == routed.get("fallback_routing_sha256") == file_sha(fallback_path),
            "Effective routing provenance SHA differs")
    require(routed.get("records") == original.get("records"),
            "Routing differs from original classification")
    source_rows = _index(original.get("records"), "pmid", "original classification")
    fallback_rows = _index(fallback.get("records"), "pmid", "full-text fallback")
    require(set(source_rows) == set(fallback_rows), "Fallback does not account for every article")
    recovered = {pmid for pmid, row in fallback_rows.items()
                 if row.get("fallback_status") == "classified" and row.get("fallback_category") == "solid"}
    continued = fallback.get("continue_pmids")
    require(isinstance(continued, list) and len(set(continued)) == len(continued) and set(continued) == recovered,
            "Fallback continuation differs from classified solid results")
    require(isinstance(original.get("solid_pmids"), list), "Original solid article list is missing")
    selected = set(original["solid_pmids"]) | recovered
    require(routed.get("solid_pmids") == [row["pmid"] for row in original["records"] if row["pmid"] in selected],
            "Effective routing article selection differs")
    rows = []
    for source in original["records"]:
        fallback_row = fallback_rows[source["pmid"]]
        require(fallback_row.get("original_status") == source.get("status")
                and fallback_row.get("original_category") == source.get("category"),
                "Fallback refers to a different original classification")
        status, category, scope = source.get("status"), source.get("category"), "metadata"
        fallback_status = fallback_row.get("fallback_status")
        require(isinstance(fallback_status, str) and bool(fallback_status),
                "Fallback classification status is missing")
        if fallback_status == "classified":
            status, category, scope = "classified", fallback_row.get("fallback_category"), "fulltext"
            require(category in {"solid", "hematologic", "non_neoplastic"}, "Invalid fallback classification")
        elif fallback_status not in {"already_solid", "already_hematologic", "not_a_safety_candidate"}:
            status, category, scope = "unresolved", None, "fulltext_fallback_unresolved"
        rows.append({**copy.deepcopy(source), "effective_status": status, "effective_category": category,
                     "classification_scope": scope, "fulltext_fallback": copy.deepcopy(fallback_row)})
    expected = {**copy.deepcopy(routed), "records": rows,
                "original_selection_sha256": file_sha(original_path),
                "base_routing_sha256": file_sha(routed_path),
                "effective_categories": dict(Counter(row["effective_category"] or "unresolved" for row in rows))}
    require(expected == effective,
            "Effective routing ledger does not reproduce from frozen sources")
    return [routed_path, fallback_path]


def _followup_classification(root, selection):
    """Verify a separately processed follow-up against its parent fallback."""
    parent = root.parent
    manifest_path = root / "batch-manifest.json"
    fallback_path = parent / "fallback-fulltext-v1/routing.json"
    original_path = parent / "classification/selection.json"
    require(manifest_path.is_file() and fallback_path.is_file() and original_path.is_file(),
            "Follow-up classification provenance is missing")
    manifest, fallback = read(manifest_path), read(fallback_path)
    require(selection.get("parent_source_run") == parent.name == manifest.get("parent_source_run")
            and manifest.get("fallback_routing_sha256") == file_sha(fallback_path),
            "Follow-up classification provenance differs")
    source_rows = _index(fallback.get("records"), "pmid", "full-text fallback")
    original_rows = _index(read(original_path).get("records"), "pmid", "original classification")
    records = _index(selection.get("records"), "pmid", "follow-up classification")
    pmids = fallback.get("continue_pmids")
    require(isinstance(pmids, list) and len(set(pmids)) == len(pmids)
            and selection.get("solid_pmids") == pmids and manifest.get("pmids") == pmids
            and set(records) == set(pmids), "Follow-up article selection differs")
    for pmid, row in records.items():
        source = source_rows.get(pmid, {})
        original = original_rows.get(pmid, {})
        require(source.get("fallback_status") == "classified" and source.get("fallback_category") == "solid"
                and row.get("status") == source["fallback_status"]
                and row.get("category") == source["fallback_category"]
                and row.get("original_category") == source.get("original_category")
                and source.get("original_category") == original.get("category")
                and source.get("original_status") == original.get("status")
                and isinstance(row.get("classification_source"), str)
                and Path(row.get("classification_source", "")).resolve() == fallback_path.resolve(),
                "Follow-up classification differs from frozen fallback")
    return [manifest_path, fallback_path, original_path]


def _check_summary(final, summary, bundle, exported):
    require(summary.get("target_written") is False and summary.get("submit_ready") is False,
            "Summary must describe an offline target preview")
    articles = _index(summary.get("articles"), "pmid", "summary article")
    require(summary.get("knowledge_records") == len(bundle["records"]),
            "Summary knowledge count differs")
    require(summary.get("compatibility_counts") == exported.get("counts")
            and summary.get("offline_ready_count") == exported.get("offline_ready_count"),
            "Summary compatibility counts differ")
    for record in bundle["records"]:
        pmid = str(record.get("article", {}).get("pmid", ""))
        require(pmid in articles, "Knowledge article missing from summary ledger")

    # Where the producing run remains available, check the ledger against its
    # frozen classification and extraction sources too.  A portable final can
    # still be inspected using its required six files alone.
    classification_path = final.parent / "classification/selection.json"
    extraction_path = final.parent / "delivery/summary.json"
    paths, classification_mode = [], "来源文件未提供的分类记录"
    if classification_path.is_file():
        root = final.parent
        original = read(classification_path)
        chosen = original
        paths.append(classification_path)
        classification_mode = "元数据原始分类"
        if original.get("scope") == "fulltext_fallback":
            paths.extend(_followup_classification(root, original))
            classification_mode = "独立全文补判分类"
        else:
            effective_path = root / "classification-routing-v2.json"
            selection_path = root / "selection.json"
            matched = False
            if selection_path.is_file():
                selection = read(selection_path)
                frozen = selection.get("requested_pmids", selection.get("selected_pmids", []))
                require(isinstance(frozen, list) and len(set(frozen)) == len(frozen),
                        "Invalid frozen article selection")
                original_solid = set(original.get("solid_pmids", [row["pmid"] for row in original["records"]
                                    if row.get("status") == "classified" and row.get("category") == "solid"]))
                if effective_path.is_file() and (effective := read(effective_path)).get("solid_pmids") == frozen:
                    routed_path = root / "classification-routing.json"
                    require(routed_path.is_file(), "Effective routing base is missing")
                    paths.extend(_effective_routing(root, original, read(routed_path), effective))
                    paths.extend([selection_path, effective_path])
                    chosen = effective
                    classification_mode = "有效分类（含全文补判）"
                    matched = True
                require(matched or not (set(frozen) - original_solid),
                        "Frozen articles beyond original classification require matching full-text routing")
        classification = _index(chosen.get("records"), "pmid", "classification")
        require(set(classification) == set(articles), "Summary and classification articles differ")
        for pmid, source in classification.items():
            row = {key: value for key, value in articles[pmid].items() if key != "extraction"}
            require(row == source, "Summary classification ledger differs")
    else:
        require(not any("effective_status" in row or "effective_category" in row for row in articles.values()),
                "Effective classification requires its frozen routing sources")
    if extraction_path.is_file():
        paths.append(extraction_path)
        extraction = read(extraction_path)
        require(summary.get("state") == extraction.get("state"), "Summary extraction state differs")
        outcomes = _index(extraction.get("article_outcomes"), "pmid", "extraction outcome")
        for pmid, outcome in outcomes.items():
            require(pmid in articles and articles[pmid].get("extraction") == outcome,
                    "Summary extraction ledger differs")
    return articles, classification_mode, paths


def load_batch(final, dictionary, dictionary_path, registry_paths=(), genes_path=DEFAULT_GENES,
               gene_cache=None):
    """Reproduce mapper/exporter output before projecting any row for the UI."""
    final = Path(final).absolute()
    values = {name: read(final / name) for name in FILES}
    bundle, exported = values["knowledge.json"], values["tumor-api-export.json"]
    mappings, report, summary = (values["target-mappings.json"],
                                 values["target-mapping-report.json"], values["summary.json"])
    require(bundle.get("schema_version") == "knowledge-candidates-v1"
            and isinstance(bundle.get("source_run"), str) and bundle["source_run"],
            "Invalid knowledge bundle identity")
    groups = _index(bundle.get("records"), "group_id", "knowledge")
    require(exported.get("source_run") == report.get("source_run") == bundle["source_run"],
            "Source runs differ")
    require(exported.get("source_bundle_sha256") == report.get("source_bundle_sha256") == digest(bundle),
            "Knowledge bundle SHA differs")
    require(exported.get("mapping_sha256") == report.get("mapping_sha256") == digest(mappings),
            "Target mappings SHA differs")
    require(exported.get("target_dictionary", {}).get("sha256") == report.get("dictionary_sha256") == digest(dictionary),
            "Target dictionary snapshot SHA differs")
    require(report.get("dictionary_snapshot_id") == dictionary["source"]["snapshot_id"],
            "Target dictionary snapshot ID differs")
    for name, value in (("export", exported), ("mapping report", report)):
        require(value.get("target_written") is False and value.get("target_contacted") is False
                and value.get("submit_ready") is False,
                f"{name} must be an offline preview")

    registry, registry_path = _registry_for(report, final, registry_paths)
    genes = _genes_for(report, genes_path, gene_cache if gene_cache is not None else {})
    calculated_mappings, calculated_report = prepare_mappings(bundle, dictionary, registry, gene_standard=genes)
    require(calculated_mappings == mappings, "Target mappings do not reproduce from frozen inputs")
    require(calculated_report == report, "Target mapping report does not reproduce from frozen inputs")
    require(export_bundle(bundle, dictionary=dictionary, mappings=mappings) == exported,
            "Target export does not reproduce from frozen inputs")
    require(set(mappings) == set(groups), "Target mapping groups differ from knowledge")

    manifest_path = final / "source-manifest.json"
    provenance = bundle.get("provenance", {})
    if provenance.get("target_dictionary_sha256"):
        require(provenance["target_dictionary_sha256"] == file_sha(dictionary_path),
                "Knowledge dictionary file SHA differs")
    if manifest_path.is_file():
        manifest = read(manifest_path)
        require(manifest.get("target_contacted") is False and manifest.get("target_written") is False,
                "Source manifest is not an offline preview")
        require(manifest.get("dictionary_sha256") == file_sha(dictionary_path),
                "Source manifest dictionary file SHA differs")
        if registry_path:
            require(manifest.get("registry_sha256") == file_sha(registry_path),
                    "Source manifest registry file SHA differs")
        else:
            require(manifest.get("registry_sha256") is None, "Source manifest registry differs")
        require(manifest.get("bundle_sha256") == provenance.get("original_extraction_bundle_sha256"),
                "Source manifest extraction bundle SHA differs")
        source = final.parent / "delivery/bundle.json"
        if source.is_file():
            require(file_sha(source) == manifest["bundle_sha256"], "Extraction bundle file SHA differs")

    candidates, receipt_path = _receipt(final, bundle)
    articles, classification_mode, ledger_paths = _check_summary(final, summary, bundle, exported)
    paths = [final / name for name in FILES]
    paths += [p for p in (receipt_path, manifest_path if manifest_path.is_file() else None,
                          registry_path) if p is not None]
    paths.extend(ledger_paths)
    return {"final": final, "bundle": bundle, "export": exported, "report": report,
            "summary": summary, "articles": articles, "candidates": candidates, "paths": paths,
            "classification_mode": classification_mode, "receipt_missing": candidates is None}


def _default_label(source_run):
    if source_run.startswith("target-positive-closure-"):
        return "独立分子适配对照"
    if any(term in source_run.lower() for term in ("fallback", "reclass", "followup", "supplement")):
        return "全文补判或补充批次"
    if source_run.startswith("ten-paper-closure-"):
        return "主批"
    return source_run


def _date_for(batches, explicit):
    if explicit:
        return datetime.fromisoformat(explicit).isoformat()
    dates = []
    for batch in batches:
        matches = re.findall(r"(?<!\d)(20\d{6})(?!\d)", batch["bundle"]["source_run"])
        for value in matches:
            try:
                dates.append(calendar_date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat())
            except ValueError:
                continue
    require(bool(dates), "Cannot infer a stable research date; supply --date")
    return max(dates)


def _assessment_counts(records):
    """Report the two saved dimensions separately, including unknown states."""
    dimensions = {"eligibility": Counter(), "execution": Counter()}
    allowed = {"eligibility": {"eligible", "deferred", "excluded"},
               "execution": {"complete", "partial", "failed"}}
    for record in records:
        assessment = record.get("assessment")
        assessment = assessment if isinstance(assessment, dict) else {}
        for dimension, counter in dimensions.items():
            value = assessment.get(dimension)
            status = value.get("status") if isinstance(value, dict) else None
            counter[status if isinstance(status, str) and status in allowed[dimension] else "unknown"] += 1
    return dimensions


def _model_stage_metrics(root, label):
    """Project only measured fields in completed model summaries, without probing hardware."""
    metrics, sources = [], []
    for relative, stage in MODEL_SUMMARIES:
        path = Path(root) / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        summary = json.loads(raw)
        if not isinstance(summary, dict) or summary.get("state") != "completed":
            continue
        sources.append({"label": f"{label} · {stage}模型摘要", "artifact": str(path.absolute()),
                        "sha256": hashlib.sha256(raw).hexdigest()})
        processed = summary.get("processed")
        if type(processed) is int and processed >= 0:
            metrics.append({"label": f"{label} · {stage} · 已处理输入", "value": processed,
                            "note": "阶段 summary 的 processed，可能包含失败或截断，不等同于有效知识数。"})
        seconds = summary.get("total_inference_seconds")
        if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
            note = "阶段 summary 的 total_inference_seconds；不是包含加载、预热和暂停的总墙钟时间。"
            scope = summary.get("timing_scope")
            if isinstance(scope, str) and scope:
                note += f" 原始计时范围：{scope}。"
            if summary.get("timing_resumed") is True:
                note += " 来源标记为断点续跑。"
            metrics.append({"label": f"{label} · {stage} · 推理累计（秒）", "value": seconds, "note": note})
        peak = summary.get("nvml_process_peak_mib")
        if type(peak) in (int, float) and math.isfinite(peak) and peak >= 0:
            note = "阶段 summary 的 NVML 采样进程显存峰值（MiB），不是整机显存使用量。"
            if summary.get("nvml_errors"):
                note += " 原始摘要记录了 NVML 采样错误，需结合来源查看。"
            if summary.get("timing_resumed") is True:
                note += " 来源标记为断点续跑。"
            metrics.append({"label": f"{label} · {stage} · NVML 进程峰值（MiB）", "value": peak, "note": note})
    return metrics, sources


def _observation_evidence(evidence):
    """Format supplied source references without asserting an independent review."""
    require(isinstance(evidence, list), "Observation evidence must be an array")
    rendered = []
    for item in evidence:
        require(isinstance(item, dict), "Invalid observation evidence")
        pieces = []
        for key, label in (("locator", "位置"), ("version_id", "版本"), ("passage_id", "段落")):
            if key in item:
                require(isinstance(item[key], str) and bool(item[key].strip()),
                        f"Invalid observation evidence {key}")
                pieces.append(f"{label} {item[key]}")
        if "start" in item or "end" in item:
            start, end = item.get("start"), item.get("end")
            require(type(start) is int and type(end) is int and 0 <= start < end,
                    "Invalid observation evidence offsets")
            pieces.append(f"字符 [{start}, {end})")
        if "quote" in item:
            require(isinstance(item["quote"], str) and bool(item["quote"].strip()),
                    "Invalid observation evidence quote")
            pieces.append(f"原文：{item['quote']}")
        require(pieces, "Observation evidence is empty")
        rendered.append("；".join(pieces))
    return rendered


def _apply_observations(path, rows):
    """Attach separate notes to exact source-run/group pairs and retain their source hash."""
    path = Path(path).absolute()
    raw = path.read_bytes()
    data = json.loads(raw)
    require(isinstance(data, dict) and isinstance(data.get("observations"), list),
            "Observations must contain an observations array")
    require(data.get("schema_version", "biomed.research-observations.v1") == "biomed.research-observations.v1",
            "Unsupported observations schema")
    row_index = {(row["source_run"], row["group_id"]): row for row in rows}
    findings = []
    for observation in data["observations"]:
        require(isinstance(observation, dict), "Invalid observation")
        run, group, note = (observation.get(key) for key in ("source_run", "group_id", "note"))
        require(all(isinstance(value, str) and value.strip() for value in (run, group, note)),
                "Observation requires source_run, group_id and nonempty note")
        require((run, group) in row_index, f"Observation references unknown source run/group: {run} / {group}")
        evidence = _observation_evidence(observation.get("evidence", []))
        description = f"独立观察：{note}"
        if "result_index" in observation or "index_base" in observation:
            index, base = observation.get("result_index"), observation.get("index_base")
            require(type(index) is int and type(base) is int and index >= base and base in (0, 1),
                    "Invalid observation result index")
            description += f"（结果索引 {index}，从 {base} 起算）"
        if evidence:
            description += " 原文依据：" + "；".join(evidence)
        row_index[(run, group)]["review_points"].append(description)
        findings.append(f"{run} / {group}：{description}")
    return findings, {"label": "独立问题观察", "artifact": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _project(batch, dictionary, label):
    bundle, exported, report = batch["bundle"], batch["export"], batch["report"]
    notes = _index(report["records"], "group_id", "mapping report")
    exports = _index(exported["records"], "group_id", "export")
    targets = {row["id"]: row for kind in ("drugs", "tumors") for row in dictionary[kind]}
    rows = []
    for original in bundle["records"]:
        group = original["group_id"]
        item, note = exports[group], notes[group]
        require(item.get("biomed_companion") == original, "Export companion differs from knowledge")
        roles = original.get("fact", {}).get("entity_roles", {})
        mapped = []
        for source in note["entity_mappings"]:
            entry = copy.deepcopy(source)
            ref = entry["entity_ref"]
            entry["roles"] = copy.deepcopy(roles.get(ref, []))
            entry["basis"] = entry.get("detail", "")
            target_id = entry.get("target_id")
            if target_id:
                require(target_id in targets, "Mapping target ID absent from dictionary")
                target = targets[target_id]
                entry["target_name"] = target.get("name_en") or target.get("name", "")
                entry["target_name_cn"] = target.get("name_cn", "")
            adopted = item.get("resolved_entities", {}).get(ref, {}).get("target_id")
            require(not adopted or adopted == target_id, "Exporter adopted a different target ID")
            entry["accepted_by_exporter"] = bool(adopted)
            mapped.append(entry)
        candidate = batch["candidates"][group]["id"] if batch["candidates"] is not None else None
        quotes = []
        seen = set()
        for anchor in original.get("anchors", []):
            if anchor.get("purpose") != "evidence":
                continue
            identity = (anchor.get("passage_id"), anchor.get("start"), anchor.get("end"))
            if identity in seen:
                continue
            seen.add(identity)
            if len(quotes) < 3:
                quotes.append({key: anchor[key] for key in ("passage_id", "locator", "start", "end", "quote")})
        adaptation = item.get("adaptation", {})
        article = original.get("article", {})
        assessment = original.get("assessment") if isinstance(original.get("assessment"), dict) else {}
        execution = assessment.get("execution") if isinstance(assessment.get("execution"), dict) else {}
        eligibility = assessment.get("eligibility") if isinstance(assessment.get("eligibility"), dict) else {}
        points = [f"来源批次：{label}（{bundle['source_run']}）。",
                  (f"执行状态：{execution.get('status') or '未记录'}；结构状态：{eligibility.get('status') or '未记录'}。"
                   "结构合格不等于语义正确。"),
                  "这里只展示最多三条证据原文；完整出处保留在已保存的候选知识中。"
                  if candidate else "这里只展示最多三条证据原文；完整出处保留在离线知识文件中，尚未取得本地导入回执。"]
        points.extend(f"待核对：{gap}" for gap in note.get("gaps", []))
        rows.append({
            "id": candidate or f"{bundle['source_run']}:{group}", "candidate_id": candidate,
            "group_id": group, "source_run": bundle["source_run"],
            "article": {key: article.get(key, "") for key in ("title", "pmid", "doi")},
            "fact": copy.deepcopy(original["fact"]), "quality": copy.deepcopy(item["quality"]),
            "execution": copy.deepcopy(execution), "eligibility": copy.deepcopy(eligibility),
            "compatibility_status": item["compatibility_status"], "offline_ready": item["offline_ready"],
            "entity_mappings": mapped, "trigger_scope": copy.deepcopy(adaptation["trigger_scope"]),
            "treatment_scope": copy.deepcopy(adaptation["treatment_scope"]),
            "target_payload": copy.deepcopy(item["target_payload"]),
            "mapping_issues": copy.deepcopy(item["mapping_issues"]),
            "contract_validation": copy.deepcopy(item["contract_validation"]),
            "matching_validation": copy.deepcopy(item.get("matching_validation", {})),
            "live_sample_matching": item.get("live_sample_matching", "unverified"),
            "review_points": points, "quotes": quotes,
        })
    return rows


def make_record(finals, dictionary_path, record_id, title, *, labels=None, registry_paths=(),
                genes_path=DEFAULT_GENES, date=None, observations_path=None):
    require(finals, "At least one final directory is required")
    dictionary_path = Path(dictionary_path).absolute()
    dictionary = read(dictionary_path)
    source = dictionary.get("source", {})
    require(source.get("read_only") is True and source.get("target_written") is False,
            "A real read-only target dictionary snapshot is required")
    require(len({Path(p).resolve() for p in finals}) == len(finals), "Duplicate final directory")
    if labels is not None:
        require(len(labels) == len(finals) and all(isinstance(s, str) and s.strip() for s in labels),
                "Supply one nonempty --label per --final")
    cache = {}
    batches = [load_batch(p, dictionary, dictionary_path, registry_paths, genes_path, cache) for p in finals]
    runs = [batch["bundle"]["source_run"] for batch in batches]
    require(len(set(runs)) == len(runs), "Each final must have a distinct source run")
    labels = list(labels) if labels is not None else [_default_label(run) for run in runs]
    all_pmids, all_rows, batch_notes, batch_labels, paths = set(), [], [], {}, [dictionary_path]
    metrics, model_sources = [], []
    totals = {"eligibility": Counter(), "execution": Counter()}
    for batch, label in zip(batches, labels, strict=True):
        run = batch["bundle"]["source_run"]
        articles = batch["articles"]
        all_pmids.update(articles)
        rows = _project(batch, dictionary, label)
        all_rows.extend(rows)
        batch_labels[run] = f"{label} · {len(articles)} 篇"
        classes = Counter((row.get("effective_status", row.get("status")),
                           row.get("effective_category", row.get("category"))) for row in articles.values())
        solid = classes[("classified", "solid")]
        classified_other = sum(count for (status, category), count in classes.items()
                               if status == "classified" and category not in (None, "solid"))
        unfinished = len(articles) - solid - classified_other
        states = _assessment_counts(batch["bundle"]["records"])
        for dimension, total in totals.items():
            total.update(states[dimension])
        eligible, execution = states["eligibility"], states["execution"]
        batch_notes.append(f"{label}（{run}）：输入 {len(articles)} 篇；{batch['classification_mode']}："
                           f"实体瘤 {solid} 篇，其他已分类 {classified_other} 篇，未完成或待定 {unfinished} 篇。"
                           f"提取记录 {len(rows)} 条：结构合格 {eligible['eligible']}、结构待处理 {eligible['deferred']}、"
                           f"结构排除 {eligible['excluded']}、结构未知 {eligible['unknown']}；"
                           f"执行完成 {execution['complete']}、部分完成 {execution['partial']}、失败 {execution['failed']}、"
                           f"未知 {execution['unknown']}；离线目标结构检查通过 {batch['export']['offline_ready_count']} 条。"
                           f"批次状态 {batch['summary'].get('state', 'unknown')}。")
        metrics.extend([{"label": f"{label} · 输入文章", "value": len(articles), "note": "按该批逐篇分类 ledger 计数，含未生成知识的文章。"},
                        {"label": f"{label} · 提取记录", "value": len(rows), "note": "包含待处理及失败占位记录，不等同于成功知识。"},
                        *[{"label": f"{label} · {text}", "value": eligible[key], "note": "直接取自结构状态；结构合格不代表语义正确。"}
                          for key, text in (("eligible", "结构合格候选"), ("deferred", "结构待处理"),
                                            ("excluded", "结构排除"), ("unknown", "结构未知"))],
                        *[{"label": f"{label} · {text}", "value": execution[key], "note": "执行状态与结构状态分别统计。"}
                          for key, text in (("complete", "执行完成"), ("partial", "执行部分完成"),
                                            ("failed", "执行失败"), ("unknown", "执行未知"))],
                        {"label": f"{label} · 离线结构通过", "value": batch["export"]["offline_ready_count"], "note": "不代表目标实际匹配、导入或临床适用。"}])
        elapsed = batch["summary"].get("elapsed_seconds")
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
            metrics.append({"label": f"{label} · 已记录耗时（秒）", "value": elapsed,
                            "note": "直接取自批次 summary，不包含未记录阶段。"})
        stage_metrics, stage_sources = _model_stage_metrics(batch["final"].parent, label)
        metrics.extend(stage_metrics)
        model_sources.extend(stage_sources)
        paths.extend(batch["paths"])
    require(len({row["id"] for row in all_rows}) == len(all_rows), "Duplicate projected candidate identities")
    observation_findings, observation_source = ([], None)
    if observations_path is not None:
        observation_findings, observation_source = _apply_observations(observations_path, all_rows)
    unique_articles = len(all_pmids)
    ready = sum(row["offline_ready"] for row in all_rows)
    missing = [str(batch["final"]) for batch in batches if batch["receipt_missing"]]
    summary = (f"{len(batches)} 个来源批次，共 {unique_articles} 篇去重文献，"
               f"{len(all_rows)} 条提取记录：结构合格 {totals['eligibility']['eligible']}、"
               f"结构待处理 {totals['eligibility']['deferred']}、结构排除 {totals['eligibility']['excluded']}、"
               f"结构未知 {totals['eligibility']['unknown']}；执行完成 {totals['execution']['complete']}、"
               f"部分完成 {totals['execution']['partial']}、失败 {totals['execution']['failed']}、"
               f"未知 {totals['execution']['unknown']}；{ready} 条离线目标结构检查通过。"
               "结构与执行状态分别统计，均不代表语义正确或真实目标匹配。")
    if missing:
        summary += " 存在缺少本地导入回执的批次，仅供离线预览。"
    paths = list(dict.fromkeys(Path(path).absolute() for path in paths))
    for model_source in model_sources:
        require(file_sha(model_source["artifact"]) == model_source["sha256"],
                "Completed model summary changed during research projection")
    if observation_source is not None:
        require(file_sha(observation_source["artifact"]) == observation_source["sha256"],
                "Observations changed during research projection")
    method_sha = file_sha(Path(__file__))
    identity = {"task_version": VERSION,
                "sample_versions": [file_sha(batch["final"] / name) for batch in batches
                                    for name in ("knowledge.json", "summary.json")]
                                   + [source["sha256"] for source in model_sources],
                "method_versions": [method_sha],
                "reference_versions": [file_sha(dictionary_path)] + [file_sha(batch["final"] / "target-mapping-report.json") for batch in batches]
                                      + ([observation_source["sha256"]] if observation_source else []),
                "completeness": "frozen"}
    record = {
        "schema_version": "research-history-v1", "id": record_id, "title": title,
        "date": _date_for(batches, date), "stage": "knowledge", "status": "unscored",
        "task_kind": "target_compatibility", "sample_count": unique_articles, "sample_unit": "篇",
        "summary": summary,
        "task": {"question": "这些真实文献的已保存知识如何映射到冻结目标契约与字典？",
                 "input": "；".join(batch_notes),
                 "outputs": ["逐篇分类与执行、结构状态统计", "目标实体、分子条件和治疗逻辑的离线兼容结果", "原始记录及可回查的原文出处"],
                 "not_measured": ["未计算提取准确率或全文召回率", "未重新调用模型或自动执行质量审核", "未向目标系统写入或验证真实样本匹配"]},
        "dataset": {"label": f"{len(batches)} 个目的性来源批次，{unique_articles} 篇去重文献",
                    "role": "compatibility_preview", "document_count": unique_articles,
                    "notes": batch_notes + ["同一 PMID 跨批出现时只计一次；元数据原始标签与独立全文补判结果按各批保留，不能用旧标签覆盖补判。",
                                            "结构状态和执行状态分别统计；结构合格仅说明可形成结构化候选，不等于语义正确。",
                                            "目的性开发及对照样本不构成独立统计留出集。"]},
        "reference": {"label": "冻结目标字典与离线结构契约", "status": "snapshot",
                      "note": "目标字典可能只包含部分变异键；结构检查、知识质量和真实匹配各自独立。"},
        "methods": [{"id": VERSION, "label": "已保存知识的离线目标兼容核对", "version": VERSION,
                     "metrics": metrics,
                     "conclusion": "所有状态来自实际批次文件；无目标写入或自动语义审核。"}],
        "findings": [summary, *batch_notes, *observation_findings],
        "next_steps": ["对未决目标条件及实体保留原始证据，按实际需要独立核对。"],
        "identity": identity,
        "provenance": [{"label": path.name, "artifact": str(path), "sha256": file_sha(path)} for path in paths]
                      + model_sources + ([observation_source] if observation_source else []),
        "registration_state": "waiting_local_import" if missing else "verified_local_imports",
        "missing_import_receipts": missing,
        "target_compatibility": {"preview_only": True, "target_written": False,
                                 "dictionary": {"snapshot_id": source["snapshot_id"],
                                                "captured_at": source.get("read_at_utc", ""),
                                                "counts": {kind: len(dictionary.get(kind, [])) for kind in ("drugs", "tumors", "rules")}},
                                 "batch_labels": batch_labels, "records": all_rows},
    }
    record = prepare_record(record)
    size = len((json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
    require(size <= MAX_BYTES, "Research preview exceeds the research page size limit; retain full artifacts and split the record")
    return record, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", type=Path, action="append", required=True,
                        help="Final artifact directory; repeat once per source run")
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", action="append", help="Optional batch label; supply once per --final")
    parser.add_argument("--registry", type=Path, action="append", default=[],
                        help="Additional frozen registry snapshot, if not in the batch root")
    parser.add_argument("--gene-standard", type=Path, default=DEFAULT_GENES)
    parser.add_argument("--date", help="Stable ISO research date when a source run has no YYYYMMDD")
    parser.add_argument("--observations", type=Path, help="Optional JSON observations keyed by source_run and group_id")
    parser.add_argument("--register", action="store_true", help="Append only after every local import is verified")
    args = parser.parse_args()
    record, missing = make_record(args.final, args.dictionary, args.id, args.title, labels=args.label,
                                  registry_paths=args.registry, genes_path=args.gene_standard, date=args.date,
                                  observations_path=args.observations)
    freeze(args.output, record)
    if args.register:
        require(not missing, "Cannot register: local import receipts missing; offline report was saved")
        result = register_record(record)
    else:
        result = {"status": "waiting_local_import" if missing else "validated_not_registered", "id": args.id}
    print(json.dumps({**result, "articles": record["sample_count"],
                      "extraction_records": len(record["target_compatibility"]["records"]),
                      "missing_import_receipts": missing, "target_written": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
