"""Register actual candidate storage results with exact source text and database links."""

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.research_history import register_record
from scripts.prepare_knowledge_intake import RUN, VERSION, freeze, read, sha


def build(root):
    bundle, prep, imported = [read(root / name) for name in ("bundle.json", "preparation.json", "import-result.json")]
    verification = read(root / "verification.json")
    if imported["dry_run"] or imported["candidate_count"] != 15 or not verification["passed"]:
        raise ValueError("Require actual import and database readback verification")
    ids = {item["group_id"]: item["id"] for item in imported["candidates"]}
    packets = {p["id"]: p for p in read(PROJECT / "var/experiments/summary-ablation-20261029/packets.json")["records"]}
    cards = []
    for row in bundle["records"]:
        packet = packets[row["group_id"]]
        central = next(p for p in packet["passages"] if p["role"] == "anchor")
        located = {a["entity_ref"] for a in row["anchors"] if a["purpose"] == "mention"}
        cards.append({
            "id": row["group_id"], "stored_candidate_id": ids[row["group_id"]],
            "pmid": row["article"]["pmid"], "title": row["article"]["title"],
            "statement": central["text"], "status": "已保存研究候选，未发布临床证据",
            "study_scope": "原有9B关系结果与Astra参考保持不变；本次验证收纳、关联和两层溯源。",
            "source_role": "英文原文，不是模型概括。", "review_note": row["assessment"]["brief"],
            "entities": [{"id": e["ref"], "type": e["kind"], "text": e["label"],
                          "standard_ids": [], "source_anchored": e["ref"] in located,
                          "normalization_status": "已保存本地候选词条；已有标准关联建议在入库详情独立保留，未确认为标准ID"}
                         for e in row["entities"]],
            "quotes": [{"passage_id": p["id"], "locator": f"{p['alias']} / {p['locator']}",
                        "quote": p["text"], "fields": ["英文原文；字段语义核验独立"],
                        "start": 0, "end": len(p["text"]), "version_id": p["version_id"],
                        "source_sha256": p["source_sha256"], "parser_version": p["parser_version"],
                        "coordinate_space": "parsed_passage_unicode_codepoints"} for p in packet["passages"]],
            "article_provenance": row["article"],
            "mapping": {"target": "biomed", "status": "research_candidate",
                        "blockers": ["候选入库不代表正式发布；关系语义仍按冻结参考记录。"],
                        "preview": {"candidate_id": ids[row["group_id"]], "fact": row["fact"],
                                    "association_counts": {kind: sum(a["association"] == kind and a["purpose"] == "mention" for a in row["anchors"])
                                                           for kind in ("model_selected", "exact_fact_text", "context_only")},
                                    "unresolved_entities": row["provenance"]["unresolved_entities"],
                                    "reference_assessment": row["assessment"]}},
        })
    source_files = ["bundle.json", "preparation.json", "import-result.json", "verification.json"]
    provenance = [{"label": name, "artifact": f"{RUN}/{name}", "sha256": sha(root / name)} for name in source_files]
    for name in ("scripts/prepare_knowledge_intake.py", "apps/curation/knowledge_storage.py", "docs/research/017-treatment-biomarker-intake.md"):
        provenance.append({"label": name, "artifact": name, "sha256": sha(PROJECT / name)})
    return {
        "schema_version": "research-history-v1", "id": RUN,
        "title": "017 · 治疗方式与生物标志物收纳及关联", "date": "2026-10-30",
        "stage": "entities", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 15, "sample_unit": "片段组",
        "summary": "同5篇15组实际保存为研究候选：106个来源内去重词条、172个已定位实体提及、441条原文定位。治疗方式与生物标志物各26个词条；零新增模型调用，关系内容核验仍为12/15，不把保存成功当作准确率提升。",
        "task": {"question": "治疗方式、生物标志物及变异的用途，能否用简单结构保存并关联到具体知识和英文出处？",
                 "input": "016实体类型整理＋014采用的无概括9B关系输出，复用冻结Astra参考。",
                 "outputs": ["实际候选库和只读查询", "来源内去重的治疗/标志物词条", "关联依据及类型/用途分离", "文章与原段两层定位"],
                 "not_measured": ["新的抽取准确率", "跨文章缩写等价确认", "新论文泛化", "正式临床证据发布"]},
        "dataset": {"label": "现有5篇15组开发回归集", "document_count": 5, "role": "存储与溯源回归",
                    "notes": ["全部英文和原始fact不变。", "没有微调、模型下载或额外推理。", "tumor_api未修改。"]},
        "reference": {"label": "沿用014冻结Astra参考", "status": "model_reference_not_human_gold",
                      "note": "本轮没有重新审核医学语义；K12/K14/K15的原错误仍保存。"},
        "methods": [{"id": "source-preserving-storage", "label": "词条＋候选关系＋原文定位", "version": VERSION,
                     "metrics": [{"label": "实际入库候选", "value": imported["candidate_count"], "unit": "组"},
                                 {"label": "来源内去重词条", "value": imported["entity_count"], "unit": "个"},
                                 {"label": "治疗方式词条", "value": 26, "unit": "个"},
                                 {"label": "生物标志物词条", "value": 26, "unit": "个"},
                                 {"label": "有原文位置的实体提及", "value": prep["stored_group_entities"], "unit": "个"},
                                 {"label": "精确原文定位", "value": imported["anchor_count"], "unit": "条"},
                                 {"label": "沿用关系内容通过", "numerator": 12, "denominator": 15, "value": "12/15"},
                                 {"label": "新增模型调用", "value": 0, "unit": "次"}],
                     "conclusion": "存储结构补齐，没有改变模型成绩。未知标准ID保留候选；按显式ID、事实字段原词、仅上下文区分关联。"}],
        "findings": [
            "ICI收为治疗类别，保留同篇定义；durvalumab仍是具体药物，二者不替换。",
            "CRP与CRP kinetics分别保存，动态变化不压缩成一个CRP指标；K08的未见显著差异结论原样保留。",
            "H3 K27M-mutant为variant；旧提取中的biomarker用途单独作为未确认候选角色，不生成DNA/HGVS或疗效关系。",
            "对HER2内部的ER等21个非独立词位置不建立定位；正常英语复数仍保留原始单数切片，不改文字。",
            "TREM2+ TAMs原本没有位置，仅保存未决项，不能用编造引用补齐。",
            "重复导入复用同一批记录；变更输入必须使用新版本，旧出处与参考结果不覆盖。",
        ],
        "next_steps": ["人工查看ICI、CRP kinetics、治疗组合等实际入库例子，决定哪些候选词条值得跨文献统一。",
                       "继续处理K12范围、K14时间、K15结论限定错误；这与本轮存储能力分开。",
                       "保持原文抽取与脚本关联，不增加概括或独立模型调用。"],
        "identity": {"task_version": VERSION, "completeness": "frozen",
                     "sample_versions": [prep["sources"]["014_packets"], prep["sources"]["016_entities"]],
                     "method_versions": [sha(root / "bundle.json"), sha(PROJECT / "apps/curation/knowledge_storage.py")],
                     "reference_versions": [prep["sources"]["014_review"]]},
        "provenance": provenance, "knowledge_candidates": cards,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / "var/experiments" / RUN)
    args = parser.parse_args()
    record = build(args.root)
    freeze(args.root / "research-record.json", record)
    print(json.dumps(register_record(record), ensure_ascii=False))
