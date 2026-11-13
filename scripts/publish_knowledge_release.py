"""Register the verified 15-group publication pilot in persistent research history."""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from apps.curation.research_history import register_record
from scripts.prepare_knowledge_intake import freeze, read, sha

RUN = "knowledge-release-20261030"
ROOT = PROJECT / "var/experiments" / RUN


def build():
    verified = read(ROOT / "verification.json")
    applied = read(ROOT / "import-result.json")
    review = read(ROOT / "astra-review.json")
    browser = read(ROOT / "browser-validation.json")
    if not verified["passed"] or applied["dry_run"] or not browser["passed"]:
        raise ValueError("Require real publication, source readback and browser verification")
    passed = verified["ai_status"].get("pass", 0)
    failed = verified["ai_status"].get("fail", 0)
    cards = []
    for row in verified["records"]:
        source = row["review_history"][-1]["result"]
        quotes = row["supporting_quotes"]
        card = {
            "id": row["group_id"], "stored_candidate_id": row["id"],
            "pmid": row["article"]["pmid"], "title": row["article"]["title"],
            "statement": next((q["quote"] for q in quotes if "results" in q.get("fields", [])), quotes[0]["quote"] if quotes else ""),
            "status": "AI通过 · 已发布 · 人工未抽查" if row["ai_status"] == "pass" else "AI未通过 · 保留候选 · 未发布",
            "study_scope": str(row["fact"].get("scope", "")),
            "source_role": "引用为英文原文；结构字段、审核意见和搜索别名分别保存。",
            "review_note": row["review"]["reason"],
            "entities": [{"id": link["entity_ref"] or link["entity_id"], "type": link["role"],
                          "text": link["label"], "standard_ids": [], "source_anchored": True,
                          "normalization_status": "本轮批准参与关系的来源内词条，非全局标准ID确认"}
                         for link in row["approved_links"]],
            "quotes": [{**{k: v for k, v in q.items() if k != "passage"},
                        "locator": q["passage"]["locator"], "version_id": row["article"]["version_id"],
                        "source_sha256": row["article"]["sha256"],
                        "parser_version": q["passage"]["parser_version"],
                        "coordinate_space": "parsed_passage_unicode_codepoints"} for q in quotes],
            "article_provenance": row["article"],
            "mapping": {"target": "biomed", "status": row["publication_status"],
                        "blockers": [row["review"]["reason"]] if row["ai_status"] == "fail" else [],
                        "preview": {"candidate_id": row["id"], "fact": row["fact"], "review": source}},
        }
        if row["publication_status"] == "published":
            card["published_knowledge_id"] = row["id"]
        cards.append(card)
    files = ["review-inputs.json", "astra-review.json", "import-result.json", "verification.json", "browser-validation.json"]
    return {
        "schema_version": "research-history-v1", "id": RUN, "title": "018 · 候选核验、发布与知识检索",
        "date": "2026-10-30", "stage": "knowledge", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 15, "sample_unit": "片段组",
        "summary": f"同5篇15组完成新的Astra独立核验：{passed}组通过并实际发布、{failed}组保留未发布。胆道癌＋durvalumab＋CRP按同一主关系AND检索命中K08/K09，英文原文与文章、段落两层溯源完整保留。",
        "task": {"question": "现有抽取结果能否通过可追溯核验，直接发布并准确检索？",
                 "input": "017实际候选库＋当前完整原文，独立Astra审核不读取旧assessment评分。",
                 "outputs": ["不可变独立审核", "AI通过自动发布、人工抽查独立", "已核验实体AND检索", "实际网页和原文双层出处"],
                 "not_measured": ["新9B抽取准确率", "新文章泛化", "纯模型推理延迟", "生产报告适用性", "全库自动抽取"]},
        "dataset": {"label": "现有5篇15组开发回归集", "document_count": 5, "role": "最小知识发布闭环",
                    "notes": ["未扩大样本、未重跑9B、未微调。", "原fact、旧assessment、441条原文位置不变。", "不改tumor_api。"]},
        "reference": {"label": "新的Astra独立发布核验", "status": "model_reference_not_human_gold",
                      "note": "实际模型参考，不是人工金标准；旧12/15不继承为发布结果。"},
        "methods": [{"id": "independent-review-and-publish", "label": "独立核验＋脚本定位校验＋已批准词条检索", "version": review["policy_version"],
                     "metrics": [{"label": "新的AI核验通过", "numerator": passed, "denominator": 15, "value": f"{passed}/15"},
                                 {"label": "实际发布", "value": passed, "unit": "条"},
                                 {"label": "未通过保留候选", "value": failed, "unit": "条"},
                                 {"label": "三条件检索命中", "value": 2, "unit": "条"},
                                 {"label": "精确原文位置保持", "value": verified["exact_original_anchors"], "unit": "条"},
                                 {"label": "新9B抽取", "value": 0, "unit": "次"}],
                     "conclusion": "知识发布与原文支持一致性单独审核；仅背景共现不参与默认检索。"}],
        "findings": [
            "K08是CRP flare/responder/nonresponder分组下DCR/ORR/PFS/OS未见显著差异，不能改为正向预测结论。",
            "K09是normal/normalized/not-normalized CRP分组的PFS结果；不能与K08的分组混合。",
            "同篇明确BTC和CRP全称后保存来源内别名；胆道癌为项目搜索译名，不冒充官方中文或英文原文。",
            "审核按实际关系批准durvalumab参与K09，保留原entity_ids不改；ICI/NLR等上下文词不自动进入主关系。",
            "独立AI、人工和发布状态；AI通过即可发布，人工报告问题撤回，历史和原文保留。",
            "本轮新审核失败原因逐条保存；旧研究成绩冻结，不能把不同核验口径的数值当成模型性能升降。",
        ],
        "next_steps": ["先查看实际检索条目及英文证据，确认适合日常知识使用。",
                       "后续在同组中修复未通过的具体绑定错误，形成新候选版本再审核；本轮不自动改写。",
                       "扩大前先验证批处理断点、成本与吞吐；本轮不扩大样本。"],
        "identity": {"task_version": "knowledge-release-v1", "completeness": "frozen",
                     "sample_versions": [sha(ROOT / "review-inputs.json")],
                     "method_versions": [sha(PROJECT / "apps/curation/knowledge_publication.py"), sha(PROJECT / "apps/curation/knowledge_search.py")],
                     "reference_versions": [sha(ROOT / "astra-review.json")]},
        "provenance": [{"label": name, "artifact": f"{RUN}/{name}", "sha256": sha(ROOT / name)} for name in files],
        "knowledge_candidates": cards,
    }


if __name__ == "__main__":
    record = build()
    freeze(ROOT / "research-record.json", record)
    print(json.dumps(register_record(record), ensure_ascii=False))
