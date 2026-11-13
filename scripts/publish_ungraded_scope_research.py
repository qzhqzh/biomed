"""Append the source-backed, isolated display study to the research history."""

import hashlib
import json
from pathlib import Path

from apps.curation.research_history import read_record, register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/ungraded-scope-display-20261112-v2"
DOC = PROJECT / "docs/research/037-ungraded-knowledge-query-v2.md"
PREVIEW = PROJECT / "var/experiments/target-identity-scope-20261112-v1/target-preview-astra-reviewed-v1.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish():
    manifest = read(ROOT / "manifest.json")
    if (not manifest["isolation_only"] or manifest["production_database_written"]
            or manifest["tumor_api_contacted"] or manifest["background_modified"]
            or manifest["source_preview_sha256"] != sha(PREVIEW) or manifest["report_sha256"] != sha(DOC)):
        raise ValueError("The isolated study or frozen sources do not match")
    for name, digest in manifest["files"].items():
        if sha(ROOT / name) != digest:
            raise ValueError("A frozen artifact has changed")
    result, browser = read(ROOT / "api-result.json"), read(ROOT / "browser-result.json")
    preview = read(PREVIEW)
    previous = read_record("target-runtime-isolation-20261112")
    earlier_display = read_record("ungraded-scope-display-20261112-v2")
    by_pmid = {row["pmid"]: row for row in result["records"]}
    browser_by_pmid = {row["pmid"]: row for row in browser["records"]}
    source_by_pmid = {row["pmid"]: row["biomed_companion"] for row in preview["records"]}
    if (set(by_pmid) != {"42719457", "42756523"} or set(browser_by_pmid) != set(by_pmid)
            or set(source_by_pmid) != set(by_pmid) or browser["write_requests"] or browser["page_errors"]
            or browser["mobile"]["width"] != browser["mobile"]["scroll"]
            or result["review_records_created"] or not result["database_query_only"]
            or result["page_sql_query_counts"] != {"1": 7, "2": 7}):
        raise ValueError("The measured outcomes do not match the study")
    descriptions = {
        "42719457": ("非小细胞肺癌 EGFR L861Q 患者一线使用达可替尼：文献报告中位 PFS 24.9 个月。", "回顾性研究"),
        "42756523": ("复发的低级别浆液性卵巢癌 KRAS G12C 病例使用 sotorasib：文献报告部分缓解。", "病例报告"),
    }
    cases = []
    for pmid in ("42719457", "42756523"):
        row, visual, source = by_pmid[pmid], browser_by_pmid[pmid], source_by_pmid[pmid]
        if (row["grade"] != "unknown" or row["drug_effect"] != "unknown"
                or row["publication_status"] != "draft" or row["ai_status"] == "pass"
                or row["candidate_search"] != "found" or row["published_search"] != "absent"
                or not all(visual[key] for key in ("ungraded", "unclassified_drug_effect", "unknown", "different", "consistent"))
                or visual["locator"] != row["source"]["locator"]):
            raise ValueError(f"The case {pmid} does not match its recorded results")
        quote = row["source"]
        cases.append({
            "id": row["group_id"], "pmid": pmid, "title": source["article"]["title"],
            "statement": descriptions[pmid][0], "status": "隔离研究候选", "study_scope": descriptions[pmid][1],
            "source_role": "同版原文", "review_note": "035 有 Astra 原文参考；本次只验证展示，隔离副本未发布。",
            "entities": [{"id": entity["ref"], "type": entity["kind"], "text": entity["label"],
                          "normalization_status": "原文候选，本次未重新评估", "standard_ids": []}
                         for entity in source["entities"] if entity["ref"] in source["fact"].get("entity_ids", [])],
            "quotes": [{"passage_id": quote["passage"], "locator": quote["locator"],
                    "quote": quote["quote"], "fields": [], "start": quote["start"], "end": quote["end"]}],
            "mapping": {"status": "隔离查询验证", "blockers": ["目标治疗报告当前不能直接消费未分级证据，见 036。"],
                        "preview": {"证据等级": "未分级", "药效": "未判定", "文献研究范围": row["reported_label"],
                                    "对照": "未提供为未知；一致或不一致都可查看", "隔离副本状态": "待核验、未发布"}},
        })
    record = {
        "schema_version": "research-history-v1", "id": "ungraded-scope-display-20261112-v3",
        "title": "037 · 未分级知识查询与文献范围对照（条件来源复核）", "date": manifest["created_at_utc"],
        "stage": "knowledge", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 2, "sample_unit": "条知识",
        "summary": "两条真实文献候选均保持未知等级与药效；不同或缺失研究范围时仍可查看并回溯原文。",
        "task": {"question": "未分级、药效未知和研究条件缺失或不一致时，biomed 能否安全展示和查询？",
                 "input": "035 的两条 Astra 原文参考候选，复制到临时 biomed 数据库。",
                 "outputs": ["候选及已发布接口的未知状态", "原文范围一致、不同、未知的页面对照", "来源和查询检查"],
                 "not_measured": ["其他文章条件抽取的准确率和覆盖率", "真实患者适用性", "目标生产报告支持", "百万文献库压力测试"]},
        "dataset": {"label": "两篇真实文献的隔离研究候选", "role": "isolated_display", "document_count": 2,
                    "notes": ["补充缺失结果段落等反例并重跑真实案例；旧版结果保留。",
                              "真实候选在临时库中保持待核验、未发布；正式发布行为另用合成测试夹具验证。",
                              "页面条件选项只是研究范围对照，没有输入患者资料，选择只存在浏览器中。"]},
        "reference": {"label": "原文定位、隔离 API 和真实浏览器行为", "status": "model_reference_not_human_gold",
                      "note": "沿用 035 的 Astra 原文参考；本次不计算模型准确率，不新增 AI 发布核验。"},
        "methods": [
            {"id": "unknowns", "label": "未知等级、药效与发布状态", "version": "biomed.knowledge-presentation.v1",
             "metrics": [{"label": "候选查询找到且保持未发布", "numerator": 2, "denominator": 2,
                          "note": "临时库按 PMID 查询；正式发布列表不包含未发布案例。"},
                         {"label": "等级和药效保持未知", "numerator": 4, "denominator": 4,
                          "note": "两条各检查两个字段；不由生存期或部分缓解推断。"}],
             "conclusion": "未知信息可展示并回查，不放宽现有发布门槛。"},
            {"id": "context", "label": "原文范围对照与来源", "version": "source-bound-context-v1",
             "metrics": [{"label": "未知、一致和不同的页面行为", "numerator": 6, "denominator": 6,
                          "note": "两条各三种选择；不同和未知均不隐藏知识。"},
                         {"label": "条件能定位到同版原文", "numerator": 2, "denominator": 2,
                          "note": "仅本次固定表达；不代表其他条件已覆盖。"},
                         {"label": "隔离列表查询数", "value": "7 → 7", "unit": "次 SQL",
                          "note": "分别取 1 条和 2 条；非全库压力测试。"}],
             "conclusion": "页面只比较明确记录的研究范围，不判断患者适用性。"},
        ],
        "findings": ["治疗结果缺少可唯一定位的原文段落时，不从其他段落背景推定治疗线，条件保持未决。",
                     "肺癌案例的一线条件引用包含 24.9 个月结果的 body/p:9，避免采用一般背景段落。",
                     "卵巢癌病例保留低级别浆液性亚型，以及既往治疗、剂量等原始信息。",
                     "未知等级或药效不推测；目标系统的治疗报告限制仍存在。",
                     "未识别的其他条件保留原文，不能据此认定文献没有条件。"],
        "next_steps": ["在更多新文章中验证必要条件识别，未识别时继续保留原始信息。",
                       "后续若需要目标生产报告消费未分级知识，应单独解决目标报告限制并评估。"],
        "identity": {"task_version": "ungraded-scope-display-v3",
                     "sample_versions": [manifest["source_preview_sha256"]],
                     "method_versions": [manifest["files"]["code/knowledge_presentation.py"],
                                         manifest["files"]["code/EvidencePresentation.tsx"]],
                     "reference_versions": [previous["content_sha256"], earlier_display["content_sha256"],
                                            sha(ROOT / "api-result.json"), sha(ROOT / "browser-result.json")],
                     "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("隔离 API 验证结果", ROOT / "api-result.json"), ("浏览器验证结果", ROOT / "browser-result.json"),
            ("实验清单", ROOT / "manifest.json"), ("研究说明", DOC))],
        "knowledge_candidates": cases,
    }
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
