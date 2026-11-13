"""Offline agreement against a blinded model reference, never human gold labels."""

import hashlib
import html
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apps.curation.decision import SCOPE_MAP, checked_input, digest, validate_decision
from apps.curation.evidence_pilot import (
    atomic_json,
    atomic_text,
    load_snapshot,
    model_result,
)

VERSION = "evidence-pilot-evaluation-v1"
REFERENCE_FILE = "astra-reference-v1.json"
REFERENCE_STATUS = "model_reference_not_human_gold"
INPUT_SCOPE = "title_abstract_mesh_publication_types"
IDENTITY_FIELDS = ("pmid", "version_id", "source_sha256", "input_sha256")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_reference(folder, documents):
    """Require one validated reference per available frozen model input."""
    path = Path(folder) / REFERENCE_FILE
    if not path.exists():
        return None
    content = path.read_bytes()
    reference = json.loads(content)
    expected_header = {
        "reference_version": "astra-reference-v1",
        "annotator_model": "gpt-6-astra",
        "status": REFERENCE_STATUS,
        "blinded_to_candidates": True,
        "input_scope": INPUT_SCOPE,
    }
    if any(reference.get(key) != value for key, value in expected_header.items()):
        raise ValueError("Unexpected model reference metadata")
    if reference["blinded_to_candidates"] is not True:
        raise ValueError("Reference blinding declaration must be true")
    if not isinstance(reference.get("created_at"), str) or not reference["created_at"]:
        raise ValueError("Reference creation time is missing")
    if not isinstance(reference.get("records"), list):
        raise TypeError("Reference records must be a list")
    inputs = {}
    for document in documents:
        if document["model_input"] is None:
            continue
        original = checked_input(document["model_input"])
        if original["pmid"] in inputs:
            raise ValueError("Duplicate source PMID in frozen inputs")
        inputs[original["pmid"]] = original
    records = {}
    for row in reference["records"]:
        pmid = row.get("pmid")
        if pmid in records:
            raise ValueError("Duplicate reference PMID")
        original = inputs.get(pmid)
        if original is None or any(
            row.get(key) != original[key] for key in IDENTITY_FIELDS
        ):
            raise ValueError("Reference source identity/hash mismatch")
        state = row.get("reference_state")
        if state not in ("determinate", "ambiguous"):
            raise ValueError("Unknown reference state")
        acceptable = row.get("acceptable_relevance")
        if (
            not isinstance(acceptable, list)
            or any(
                not isinstance(item, str) or item not in SCOPE_MAP
                for item in acceptable
            )
            or len(set(acceptable)) != len(acceptable)
        ):
            raise ValueError("Invalid acceptable relevance labels")
        if not isinstance(row.get("notes"), str):
            raise TypeError("Reference notes must be a string")
        validated = validate_decision(
            json.dumps(row["raw_decision"], ensure_ascii=False), original["metadata"]
        )
        records[pmid] = {**row, "validated": validated}
    if set(records) != set(inputs):
        raise ValueError("Missing reference records for frozen model inputs")
    return {
        "file_sha256": hashlib.sha256(content).hexdigest(),
        "metadata": {key: reference[key] for key in (*expected_header, "created_at")},
        "records": records,
    }


def _manifest(folder):
    path = folder / "model" / "manifest.json"
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text())
    if digest(manifest["config"]) != manifest["config_sha256"]:
        raise ValueError("Model manifest configuration hash mismatch")
    return manifest


def _scope_outcome(predicted, reference, eligible, failed=False):
    if not eligible:
        return "unscored"
    if failed or predicted not in set(SCOPE_MAP.values()):
        return "failed"
    if predicted == "uncertain":
        return "abstained"
    return "matched" if predicted == reference else "disagreed"


def _timing(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _record(folder, cohort, document, reference):
    model = model_result(folder, document)
    valid_model = model["status"] == "validated_candidate"
    decision = model.get("decision") or {} if valid_model else {}
    baseline = document.get("rule_baseline") or {}
    validated = reference["validated"] if reference else {}
    reference_state = (
        reference["reference_state"] if reference else "source_unavailable"
    )
    reference_scope = validated.get("scope")
    determinate = reference_state == "determinate"
    eligible = determinate and reference_scope != "uncertain"
    relevance = validated.get("relevance")
    rule_scope = baseline.get("scope")
    model_scope = decision.get("scope")
    model_relevance = decision.get("relevance")
    relevance_outcome = (
        "unscored"
        if not determinate
        else "failed"
        if not valid_model
        else "matched"
        if model_relevance == relevance
        else "disagreed"
    )
    ambiguous = reference_state == "ambiguous"
    acceptable = reference["acceptable_relevance"] if reference else []
    acceptable_scopes = {SCOPE_MAP[item] for item in acceptable} - {"uncertain"}
    result_path = folder / "model" / f"result-{document['pmid']}.json"
    return {
        "cohort": cohort,
        "rank": document["rank"],
        "pmid": document["pmid"],
        "title": document["title"],
        "source_scope": document["input_scope"],
        "classification_input_scope": INPUT_SCOPE,
        "reference_state": reference_state,
        "reference_relevance": relevance,
        "reference_scope": reference_scope,
        "reference_notes": reference["notes"] if reference else "No frozen model input",
        "reference_quotes": validated.get("evidence", []),
        "reference_labels": validated.get("labels", []),
        "acceptable_relevance": acceptable,
        "scope_eligible": eligible,
        "rule_scope": rule_scope,
        "rule_labels": baseline.get("labels", []),
        "model_scope": model_scope,
        "model_relevance": model_relevance,
        "model_status": model["status"],
        "model_error": model.get("error", ""),
        "model_seconds": _timing(model.get("seconds")),
        "model_labels": decision.get("labels", []),
        "model_result_path": str(result_path) if result_path.exists() else None,
        "model_raw_output_field": "raw_output" if result_path.exists() else None,
        "rule_outcome": _scope_outcome(rule_scope, reference_scope, eligible),
        "model_outcome": _scope_outcome(
            model_scope, reference_scope, eligible, not valid_model
        ),
        "model_relevance_outcome": relevance_outcome,
        "rule_acceptable_scope": (
            rule_scope in acceptable_scopes
            if ambiguous and rule_scope in set(SCOPE_MAP.values()) - {"uncertain"}
            else None
        ),
        "model_acceptable_scope": (
            model_scope in acceptable_scopes
            if ambiguous and valid_model and model_scope != "uncertain"
            else None
        ),
        "model_acceptable_relevance": (
            model_relevance in acceptable if ambiguous and valid_model else None
        ),
    }


def _counts(rows, key):
    counts = Counter(row[key] for row in rows)
    return {
        "matched": counts["matched"],
        "denominator": sum(
            counts[state] for state in ("matched", "disagreed", "abstained", "failed")
        ),
        "abstained": counts["abstained"],
        "failed": counts["failed"],
        "disagreed": counts["disagreed"],
        "unscored": counts["unscored"],
    }


def _cohort(folder, frozen, rows, manifest):
    snapshot = frozen["snapshot"]
    config = manifest.get("config", {})
    timings = [row["model_seconds"] for row in rows if row["model_seconds"] is not None]
    rules = {
        "id": "rules",
        "label": "规则基线",
        **_counts(rows, "rule_outcome"),
        "mean_seconds": None,
        "training": False,
        "version": snapshot["taxonomy"].get("version"),
        "taxonomy_sha256": snapshot["taxonomy_sha256"],
    }
    model = {
        "id": "local_model",
        "label": "本地小模型",
        **_counts(rows, "model_outcome"),
        "mean_seconds": round(sum(timings) / len(timings), 6) if timings else None,
        "timed_documents": len(timings),
        "timing_scope": "all_cohort_attempts_with_timing_including_failures",
        "training": False,
        "version": config.get("version"),
        "model_id": config.get("model_id"),
        "profile": config.get("profile"),
        "config_sha256": manifest.get("config_sha256"),
        "manifest_sha256": file_hash(folder / "model" / "manifest.json")
        if manifest
        else None,
    }
    return {
        "id": folder.name,
        "label": folder.name,
        "n": len(rows),
        "scope_eligible": sum(row["scope_eligible"] for row in rows),
        "reference_ambiguous": sum(
            row["reference_state"] == "ambiguous" for row in rows
        ),
        "reference_source_unavailable": sum(
            row["reference_state"] == "source_unavailable" for row in rows
        ),
        "reference_uncertain_scope": sum(
            row["reference_state"] == "determinate"
            and row["reference_scope"] == "uncertain"
            for row in rows
        ),
        "methods": [rules, model],
        "model_relevance": {
            "id": "local_model_relevance",
            "label": "小模型相关性一致率（不与规则比较）",
            **_counts(rows, "model_relevance_outcome"),
            "training": False,
        },
        "model_statuses": dict(Counter(row["model_status"] for row in rows)),
    }


def _markdown(evaluation):
    def cell(value):
        return (
            html.escape(str(value))
            .replace("|", "\\|")
            .replace("\n", " ")
            .replace("\r", " ")
        )

    local_time = datetime.fromisoformat(evaluation["generated_at"]).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    lines = [
        "# 文献分类离线参考评估",
        "",
        f"生成时间：{local_time.isoformat()}（北京时间）。",
        "",
    ]
    if evaluation["reference_status"] == "waiting_reference":
        lines.extend(
            ["状态：waiting_reference。参考文件尚未全部到齐，不产生一致率分数。", ""]
        )
        lines.extend(f"- {cell(path)}" for path in evaluation["missing_references"])
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "参考来自独立模型，未经过人工金标准确认；一致率只描述与该参考的符合程度。",
            "范围计分仅包含参考明确且映射为非 uncertain 的条目；uncertain 预测计为拒判。协议失败与未运行保留在适用分母。",
            "含糊参考和范围未细分/范围外条目另列，不加入范围一致率。多标签非穷尽参考，不计算标签精确率。",
            "未训练模型，未发布正式知识。",
            "",
            "| 样本组 | 方法 | 范围一致 / 分母 | 拒判 | 失败 | 不一致 |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for cohort in evaluation["cohorts"]:
        for method in cohort["methods"]:
            lines.append(
                f"| {cell(cohort['label'])} | {method['label']} | {method['matched']} / {method['denominator']} | {method['abstained']} | {method['failed']} | {method['disagreed']} |"
            )
        relevance = cohort["model_relevance"]
        lines.extend(
            [
                "",
                f"{cell(cohort['label'])}：{cohort['n']} 篇，含糊参考 {cohort['reference_ambiguous']} 篇；小模型单独相关性一致 {relevance['matched']} / {relevance['denominator']}，失败 {relevance['failed']}。",
                "",
            ]
        )
    lines.extend(
        [
            "## 逐篇结果",
            "",
            "| 样本组 | 序号 / PMID | 参考相关性 | 参考状态 | 规则范围结果 | 模型范围结果 | 模型运行状态 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in evaluation["records"]:
        values = (
            row["cohort"],
            f"{row['rank']} / {row['pmid']}",
            row["reference_relevance"],
            row["reference_state"],
            row["rule_outcome"],
            row["model_outcome"],
            row["model_status"],
        )
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "精确引句、可接受标签、未计分条目的路由、模型原始结果位置及来源哈希均保存在 evaluation.json。",
        ]
    )
    return "\n".join(lines) + "\n"


def evaluate(experiments, output):
    """Validate every input before writing either evaluation artifact."""
    folders = [Path(folder).resolve() for folder in experiments]
    if not folders or len({folder.name for folder in folders}) != len(folders):
        raise ValueError("Provide experiment directories with distinct names")
    output = Path(output).resolve()
    prepared, provenance, missing = [], [], []
    for folder in folders:
        frozen = load_snapshot(folder)
        snapshot = frozen["snapshot"]
        if digest(snapshot["taxonomy"]) != snapshot["taxonomy_sha256"]:
            raise ValueError("Frozen taxonomy hash mismatch")
        reference = load_reference(folder, snapshot["documents"])
        reference_path = folder / REFERENCE_FILE
        manifest = _manifest(folder)
        prepared.append((folder, frozen, reference, manifest))
        provenance.append(
            {
                "cohort": folder.name,
                "snapshot_path": str(folder / "snapshot.json"),
                "snapshot_sha256": frozen["sha256"],
                "snapshot_file_sha256": file_hash(folder / "snapshot.json"),
                "reference_path": str(reference_path),
                "reference_sha256": reference["file_sha256"] if reference else None,
                "reference_metadata": reference["metadata"] if reference else None,
            }
        )
        if reference is None:
            missing.append(str(reference_path))
    result = {
        "schema_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_status": "waiting_reference" if missing else REFERENCE_STATUS,
        "comparison_task": "document_disease_scope",
        "comparison_input_scope": INPUT_SCOPE,
        "state": "waiting_reference" if missing else "evaluated",
        "cohort_sources": provenance,
        "cohorts": [],
        "records": [],
        "training": False,
        "published_evidence": 0,
    }
    if missing:
        result["missing_references"] = missing
        result["cohorts"] = [
            {
                "id": folder.name,
                "label": folder.name,
                "n": len(frozen["snapshot"]["documents"]),
                "scope_eligible": None,
                "reference_ambiguous": None,
                "methods": [],
            }
            for folder, frozen, _, _ in prepared
        ]
    else:
        for folder, frozen, reference, manifest in prepared:
            rows = [
                _record(
                    folder,
                    folder.name,
                    document,
                    reference["records"].get(document["pmid"]),
                )
                for document in frozen["snapshot"]["documents"]
            ]
            result["cohorts"].append(_cohort(folder, frozen, rows, manifest))
            result["records"].extend(rows)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "evaluation.json", result)
    atomic_text(output / "evaluation.md", _markdown(result))
    return result
