"""Publish measured independent entity/relation summary-assistance comparisons."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.core_knowledge_optional_entities import _located
from apps.curation.research_history import prepare_record, read_record, register_record
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import read, sha
from scripts.publish_summary_ablation import ratio

RID = "summary-assist-20261029"
ROOT = PROJECT / "var/experiments" / RID
DOC = "docs/research/015-summary-assist.md"
ARMS = {
    "source_only": "A · 直接依据原文抽取",
    "summary_assisted": "B · 原文加机器概括辅助",
}
STAGES = {"entities": "实体", "relations": "关系"}


def arm_method(evaluation, arm):
    entity = evaluation["scores"]["entities"][arm]
    relation = evaluation["scores"]["relations"][arm]
    timing = evaluation["timing"]
    assisted = arm == "summary_assisted"
    prefix = "B" if assisted else "A"
    runs = [timing["runs"][f"{prefix}_{stage}"] for stage in STAGES]
    if assisted:
        runs.append(timing["runs"]["summary"])
    metrics = [
        ratio("实体核心可用（共同脚本后）", entity["effective_core_usable"],
              note="同一原文定位、类型保护及投影作用于各臂自身输出；新增概括不参与定位。"),
        ratio("实体核心可用（模型原始输出）", entity["raw_core_usable"],
              note="单列尚未经过共同脚本处理的原始模型质量。"),
        ratio("当前具体实体身份完整", entity["current_identity_complete"]),
        ratio("历史52项身份覆盖", entity["historical_52_items"] - entity["historical_52_missing_items"],
              entity["historical_52_items"], "固定历史口径；不能替代当前实体类型和具体药物身份评估。"),
        ratio("关系共同字段核心可用", relation["effective_core_usable"],
              note="人群、具体药物、条件、结论与数值绑定由Astra对照原文审核。"),
        ratio("关系原始结构且核心通过", relation["strict_core_ready"],
              note="原始输出同时满足严格共同字段结构与核心语义。"),
        ratio("关系诊断后核心就绪", relation["diagnostic_core_ready"],
              note="仅使用两臂相同的格式、大小写及附加实体诊断。"),
        ratio("关系核心可用且聚焦ANCHOR", relation["core_and_anchor"]),
    ]
    for stage, cost_key in (("entities", "summary_plus_entities_seconds"),
                            ("relations", "summary_plus_relations_seconds")):
        seconds = timing[cost_key] if assisted else timing["runs"][f"A_{stage}"]["inference_seconds"]
        metrics.append({"label": f"{STAGES[stage]}阶段成本", "value": round(seconds, 2), "unit": "秒",
                        "note": "含该阶段使用的摘要生成成本。" if assisted else "复用历史同配置单次推理测量。"})
    total_key = "summary_assisted_total_seconds" if assisted else "source_only_total_seconds"
    metrics.extend([
        {"label": "两阶段合计推理", "value": round(timing[total_key], 2), "unit": "秒",
         "note": "概括成本只计一次；两个独立阶段的成本相加，不是完整串联流程测速。"},
        {"label": "合计输出tokens", "value": sum(r["output_tokens"] for r in runs), "unit": "tokens",
         "note": "辅助臂含独立概括输出一次。"},
        {"label": "进程显存峰值", "value": max(
            (r["peak_process_mib"] for r in runs if r["peak_process_mib"] is not None), default="未记录"),
         "unit": "MiB", "note": "各顺序运行进程的最大实测NVML峰值，不叠加。"},
    ])
    return {
        "id": arm, "label": ARMS[arm], "version": evaluation["version"], "metrics": metrics,
        "conclusion": "实体、关系分别对照；关系输入始终固定010实体清单，未使用本轮新抽出的实体。载入、预热及Astra审核不计入上述推理时间。",
    }


def paired_method(evaluation, stage):
    pairs = evaluation["paired"][stage]
    metrics = [ratio(label, pairs["pairs"][key]["count"],
                     note="组别：" + ("、".join(pairs["pairs"][key]["ids"]) or "无"))
               for key, label in (("both_pass", "两臂均通过"), ("source_only", "仅A通过"),
                                  ("summary_assisted_only", "仅B通过"), ("both_fail", "两臂均未通过"))]
    metrics.extend([
        {"label": "概括辅助净增核心通过", "value": pairs["net_group_gain"], "unit": "组"},
        {"label": "新增核心错绑", "value": len(evaluation["new_core_misbindings"][stage]), "unit": "项",
         "note": "按冻结参考核对，不能以召回增加抵消新错误。"},
    ])
    return {"id": f"paired-{stage}", "label": f"{STAGES[stage]}独立阶段：配对结果",
            "version": evaluation["version"], "metrics": metrics,
            "conclusion": "同15组、5篇开发回归样本；净增数量和出错变化分别报告，不外推全库。"}


def card(packet, row):
    anchors = [p for p in packet["passages"] if p["role"] == "anchor"]
    require(len(anchors) == 1 and bool(anchors[0]["text"]), "Need one original ANCHOR")
    require(row["summary"]["counts_as_source"] is False, "Summary must never be evidence")
    require(row["original_passages"] == packet["passages"], "Card original passages changed")
    source = {p["id"]: p["text"] for p in packet["passages"]}
    entities = []
    for entity in packet["entities"]:
        candidate = (entity.get("link") or {}).get("candidate_id")
        entities.append({
            "id": entity["id"], "type": entity["type"], "text": entity["text"],
            "normalization_status": "固定关系输入中的标准候选，非本轮确认" if candidate else "固定关系输入提示，未确认标准关联",
            "standard_ids": [candidate] if candidate else [], "source_anchored": _located(entity, source),
        })
    notes = ["主文为英文原始ANCHOR；实体表是两臂关系抽取共享的固定输入提示，不是本轮新实体结果。"]
    preview = {}
    for stage, stage_label in STAGES.items():
        preview[f"{stage_label}独立对照"] = {}
        for arm, arm_label in ARMS.items():
            result = row[stage][arm]
            notes.append(f"{stage_label}{arm_label}：核心{'通过' if result['core_usable'] else '未通过'}。")
            detail = {"原始模型输出": result["raw_result"], "Astra原文审核": result["review"]}
            if stage == "entities":
                detail["原文定位与共同脚本检查"] = {
                    "原文定位": result["source_entity_locations"],
                    "共同投影检查": result["common_projection_checks"],
                    "标准关联正确率": result["wrong_links"],
                }
            else:
                detail["结构与脚本诊断"] = result["mechanical"]
            preview[f"{stage_label}独立对照"][arm_label] = detail
    preview["机器概括（辅助输入，绝非证据）"] = row["summary"]
    preview["独立阶段耗时（秒，不含共享概括）"] = {
        stage: row[stage]["inference_seconds"] for stage in STAGES
    }
    return {
        "id": packet["id"], "pmid": packet["article"]["pmid"], "title": packet["article"]["title"],
        "statement": anchors[0]["text"], "status": "研究对照，未导出知识",
        "review_note": " ".join(notes),
        "study_scope": "实体与关系分别评估，关系未使用本轮新实体；非端到端知识导出。",
        "source_role": "英文原段为唯一来源；所有上下文展示不代表模型已逐字段引用。",
        "entities": entities,
        "quotes": [{
            "passage_id": p["id"], "locator": f"{p['alias']} / {p['role'].upper()} / 研究原文上下文 · {p['locator']}",
            "quote": p["text"], "fields": ["研究输入上下文，非自动认定的字段支持"],
            "start": 0, "end": len(p["text"]), "version_id": p["version_id"],
            "source_sha256": p["source_sha256"], "parser_version": p["parser_version"],
            "coordinate_space": "parsed_passage_unicode_codepoints",
        } for p in packet["passages"]],
        "article_provenance": packet["article"],
        "mapping": {"status": "not_run", "blockers": [
            "本轮只比较独立阶段，未做新实体到新关系的完整串联，也未导出生产知识。",
            "机器概括不作证据；标准候选ID正确性未在本轮重新测量。",
        ], "preview": preview},
    }


def build(root, conclusions_path):
    root, conclusions_path = Path(root), Path(conclusions_path)
    evaluation, conclusions = read(root / "evaluation.json"), read(conclusions_path)
    require(evaluation["denominator"] == 15 and evaluation["state"] == "research_comparison_not_production",
            "Expected completed 15-group independent comparison")
    require(evaluation["timing"]["summary_shared_count_in_total"] == 1, "Summary cost must be counted once")
    require(isinstance(conclusions.get("summary"), str) and bool(conclusions["summary"].strip()),
            "Supply measured summary")
    for field in ("findings", "next_steps"):
        require(isinstance(conclusions.get(field), list)
                and all(isinstance(x, str) and x.strip() for x in conclusions[field]),
                f"Supply measured conclusions {field}")
    for source in evaluation["sources"]:
        path = Path(source["path"])
        require(sha(path if path.is_absolute() else PROJECT / path) == source["sha256"],
                f"Evaluated source changed: {path}")
    packets = read(root / "packets.json")["records"]
    rows = indexed(evaluation["records"], "evaluation")
    require(len(packets) == len(rows) == 15 and {p["id"] for p in packets} == set(rows), "Paired card IDs differ")
    require(len({p["article"]["pmid"] for p in packets}) == 5, "Expected five original articles")
    for stage in STAGES:
        for arm in ARMS:
            require(sum(row[stage][arm]["core_usable"] for row in rows.values())
                    == evaluation["scores"][stage][arm]["effective_core_usable"], "Core score/card totals differ")
    methods = [arm_method(evaluation, arm) for arm in ARMS]
    methods.extend(paired_method(evaluation, stage) for stage in STAGES)
    methods.append({
        "id": "orientation-diagnostic", "label": "机器概括自身诊断与预登记判定", "version": evaluation["version"],
        "metrics": [
            ratio("概括自身有原文支持", evaluation["summary_quality"]["supported_groups"],
                  note="仅诊断概括忠实度；不能证明它提高了抽取效果。"),
            {"label": "概括生成推理", "value": round(evaluation["timing"]["runs"]["summary"]["inference_seconds"], 2), "unit": "秒"},
            {"label": "预登记默认启用条件", "value": "达到" if evaluation["decision"]["default_summary_enabled"] else "未达到",
             "note": "只表示本批开发样本是否达到预设工程收益条件，不是显著性或通用有效性结论。"},
        ], "conclusion": "原文始终保留；摘要质量、下游质量和包含摘要的总成本分别评估。",
    })
    artifacts = ["evaluation.json", "reference.json", "review.json", "plan.json", "packets.json",
                 "summary-definition.json", "summary-inputs.json", "entity-definition.json", "entity-inputs.json",
                 "relation-definition.json", "relation-inputs.json", "assisted-inputs-manifest.json"]
    artifacts += [name for name in ("executed-code.json", "summary-preflight.json", "assisted-preflight.json") if (root / name).exists()]
    provenance = [{"label": name, "artifact": f"{RID}/{name}", "sha256": sha(root / name)} for name in artifacts]
    for name in (DOC, "scripts/publish_summary_assist.py", "scripts/evaluate_summary_assist.py", "scripts/summary_assist_pilot.py"):
        provenance.append({"label": name, "artifact": name, "sha256": sha(PROJECT / name)})
    provenance.append({"label": "实测结论", "artifact": str(conclusions_path), "sha256": sha(conclusions_path)})
    prior = PROJECT / "apps/curation/research_data" / f"{RID}.json"
    date = read_record(RID)["date"] if prior.exists() else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    return prepare_record({
        "schema_version": "research-history-v1", "id": RID, "title": "015 · 先概括能否改善实体与关系抽取",
        "date": date, "stage": "entities", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 15, "sample_unit": "片段组", "summary": conclusions["summary"],
        "task": {
            "question": "独立生成的短概括能否改善实体、关系抽取，且收益是否抵得过新增推理成本？",
            "input": "同5篇15组英文原文、同9B；B在原始输入前增加共享概括，原系统提示与原文保持不变。",
            "outputs": ["实体、关系各自的核心质量、原始输出与共同脚本后结果", "配对改善、退步、错误与遗漏", "包含一次概括成本的合计推理耗时", "英文原文、原始输出及Astra参考审核"],
            "not_measured": ["新实体串入新关系的完整流程", "全库质量与统计显著性", "标准ID正确率与人工审核耗时", "生产知识库导入"],
        },
        "dataset": {"label": "同5篇15组开发回归集", "document_count": 5, "role": "开发回归集",
                    "notes": ["A复用010实体及014无概括关系结果；B新增15次概括、15次实体和15次关系计分调用。",
                              "关系两臂始终共享旧实体输入；实体B的输出未传给关系B。",
                              "主文是原始英文ANCHOR，机器概括只在展开内容中标记为辅助输入。",
                              "每臂单次测量，历史基线与新增运行不构成随机重复实验。"]},
        "reference": {"label": "Astra冻结原文参考及逐组审核", "status": "model_reference_not_human_gold",
                      "note": "原文是唯一证据；Astra模型参考不是人工金标准。概括不得补充原文没有的实体、关系或条件。"},
        "methods": methods, "findings": conclusions["findings"], "next_steps": conclusions["next_steps"],
        "identity": {"task_version": evaluation["version"], "completeness": "frozen",
                     "sample_versions": [sha(root / name) for name in ("packets.json", "summary-inputs.json", "entity-inputs.json", "relation-inputs.json")],
                     "method_versions": [sha(root / name) for name in ("evaluation.json", "summary-definition.json", "entity-definition.json", "relation-definition.json")] + [sha(conclusions_path)],
                     "reference_versions": [sha(root / "reference.json"), sha(root / "review.json")]},
        "provenance": provenance, "knowledge_candidates": [card(packet, rows[packet["id"]]) for packet in packets],
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--conclusions", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(register_record(build(args.root, args.conclusions)), ensure_ascii=False))
