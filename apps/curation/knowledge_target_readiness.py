"""Explain target fit separately from extraction quality and missing identities.

This routes the *stored fact*, not the whole article. It never manufactures a
trigger, relaxes the target contract, calls a model, or writes either database.
"""

import json
import re
from collections import Counter

from apps.curation.knowledge_eligibility import assess_record

VERSION = "biomed.target-readiness.v1"
GENOMIC = re.compile(
    r"\b(?:mutations?|mutated|mutants?|variants?|amplifications?|deletions?|fusions?|"
    r"rearrangements?|frameshifts?|exons?|cnv|copy[- ]number|wild[- ]type|ctdna|vaf)\b"
    r"|(?<!\w)[cnpg]\.[0-9]+|\b[A-Z][A-Z0-9]+::[A-Z][A-Z0-9]+\b",
    re.IGNORECASE,
)
EXPRESSION = re.compile(r"\b(?:expression|overexpress\w*|upregulat\w*|downregulat\w*|protein|serum|signature)\b", re.IGNORECASE)
MECHANISM = re.compile(r"\b(?:in vitro|mechanis\w*|axis|transcription\w*|signaling|proliferation)\b", re.IGNORECASE)
SUBSTITUTION = re.compile(r"(?<!\w)(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*](?!\w)")
MESSAGES = {
    "structure_incomplete": "提取结构仍有缺口，先处理提取问题；不计入目标映射成功率。",
    "offline_validated": "已有明确条件与完整映射，目标离线结构通过；未导入生产。",
    "entity_sync_required": "其他离线检查通过，等待自动新增实体并回填目标ID。",
    "preview_validated": "隔离预览库已验证，不能视为生产目标ID就绪。",
    "no_genomic_condition": "本条知识及所引片段没有明确的突变、CNV或融合适用条件，当前证据中心不能表达；补药物或癌种ID不能解决。",
    "expression_not_genomic_event": "本条是表达量、蛋白或表达签名相关知识，不能转换成突变、CNV或融合入口。",
    "mechanism_not_genomic_event": "本条描述分子机制或体外功能，基因参与机制不等于存在可用于匹配的基因变异条件。",
    "explicit_target_scope_mismatch": "已确认的知识范围超出当前证据中心的靶点表达能力，保留在biomed。",
    "genomic_mapping_unresolved": "存在基因组事件或条件线索，需核对抽取和映射；不能按普通机制知识排除。",
    "scope_unresolved": "现有事实或引用不足以确定目标适用范围，保留待处理。",
}


def has_genomic_signal(text):
    if GENOMIC.search(text):
        return True
    return any(not re.search(r"(?:fig(?:ure)?s?\.?|table|stage)\s*\(?\s*$", text[max(0, m.start()-20):m.start()], re.IGNORECASE)
               for m in SUBSTITUTION.finditer(text))


def target_readiness(record, compatibility_status=None):
    assessment = record.get("assessment") or {}
    status = compatibility_status or assessment.get("compatibility", {}).get("status")

    def result(bucket, reason):
        return {"version": VERSION, "status": bucket, "reason_code": reason,
                "reason": MESSAGES[reason], "scope": "stored_fact_and_cited_passages",
                "semantic_correctness": "unassessed", "target_written": False}

    if assess_record(record)["status"] != "eligible":
        return result("extraction_gap", "structure_incomplete")
    if status == "offline_validated":
        return result("offline_ready", "offline_validated")
    if status == "ready_after_entity_sync":
        return result("entity_sync", "entity_sync_required")
    if status == "preview_validated":
        return result("preview_only", "preview_validated")
    if status == "not_representable":
        return result("outside_target", "explicit_target_scope_mismatch")
    # A failed/partial extraction or missing source must not be confidently
    # diagnosed as evidence that the target cannot express the original study.
    if assessment.get("execution", {}).get("status") != "complete" or status == "invalid_source":
        return result("mapping_needed", "scope_unresolved")
    evidence = [a for a in record.get("anchors", []) if a.get("purpose") == "evidence"]
    if not evidence or any(not isinstance(a.get("quote"), str) or not a["quote"]
                           or type(a.get("start")) is not int or type(a.get("end")) is not int
                           or a["start"] < 0 or a["end"] - a["start"] != len(a["quote"])
                           for a in evidence):
        return result("mapping_needed", "scope_unresolved")
    fact = record.get("fact") or {}
    text = json.dumps({k: fact.get(k) for k in (
        "population", "treatment", "comparator", "conditions", "results", "scope", "conclusion"
    )}, ensure_ascii=False)
    quotes = "\n".join(a["quote"] for a in evidence)
    # An event appearing in the source but missed by extraction is a possible
    # extraction gap, never grounds for closing the case as non-applicable.
    entity_text = " ".join(str(e.get("label", "")) for e in record.get("entities", []))
    if has_genomic_signal(text + "\n" + quotes + "\n" + entity_text):
        return result("mapping_needed", "genomic_mapping_unresolved")
    if EXPRESSION.search(text) and EXPRESSION.search(quotes):
        return result("outside_target", "expression_not_genomic_event")
    if fact.get("relation") == "mechanism" and MECHANISM.search(quotes):
        return result("outside_target", "mechanism_not_genomic_event")
    roles = fact.get("entity_roles")
    if isinstance(roles, dict) and not any("molecular" in values for values in roles.values() if isinstance(values, list)):
        return result("outside_target", "no_genomic_condition")
    if MECHANISM.search(text) and MECHANISM.search(quotes):
        return result("outside_target", "mechanism_not_genomic_event")
    return result("mapping_needed", "scope_unresolved")


def readiness_summary(records, statuses=None):
    statuses = statuses or {}
    rows = [{"group_id": r["group_id"], "pmid": r.get("article", {}).get("pmid"),
             **target_readiness(r, statuses.get(r["group_id"]))} for r in records]
    counts = Counter(r["status"] for r in rows)
    reasons = Counter(r["reason_code"] for r in rows if r["status"] == "outside_target")
    return {"version": VERSION, "counts": dict(counts), "outside_target_reasons": dict(reasons),
            "structurally_eligible": len(rows) - counts["extraction_gap"], "records": rows,
            "note": "分母为结构合格知识；不适用于当前证据中心不代表原知识无效。"}
