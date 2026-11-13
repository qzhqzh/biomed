"""Publish the measured source-only refinement without changing production evidence."""

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.research_history import prepare_record, read_record, register_record
from scripts.evaluate_core_knowledge import require
from scripts.evaluate_knowledge_candidates import read, sha
from scripts.publish_summary_ablation import ratio

RID = "source-refinement-20261030"
DOC = "docs/research/016-source-refinement.md"
ARMS = {"baseline": "原文基线", "refined": "本轮简化调整"}


def card(packet, row):
    anchor = next(p for p in packet["passages"] if p["role"] == "anchor")
    require(row["original_passages"] == packet["passages"], "Original passages changed")
    details = {
        "实体脚本类型调整（未重跑实体模型）": row["entities"],
        "关系原文基线": row["relations"]["baseline"],
        "关系三条约束": row["relations"]["refined"],
        "错误变化": row["error_changes"],
    }
    return {
        "id": packet["id"], "pmid": packet["article"]["pmid"],
        "title": packet["article"]["title"], "statement": anchor["text"],
        "status": "独立阶段研究，未导出生产知识",
        "study_scope": "实体规则回放与关系提示对照分别评价；关系仍使用相同旧实体库存。",
        "source_role": "英文原始段落；原文及出处未改写。",
        "review_note": "下方实体表展示脚本类型视图；本轮关系仍使用旧库存。关系内容审核：" + "；".join(
            label + ("通过" if row["relations"][arm]["core_usable"] else "未通过")
            for arm, label in ARMS.items()
        ) + "。类型变化与原始模型输出见展开详情。",
        "entities": [{
            "id": e["id"], "type": e["type"], "text": e["text"],
            "standard_ids": [(e.get("link") or {})["candidate_id"]] if (e.get("link") or {}).get("candidate_id") else [],
            "source_anchored": bool(e.get("locations")),
            "normalization_status": "规则派生类型；原始类型见详情，未重新确认标准ID" if e.get("type_refinement") else "沿用原实体；本轮未重新确认标准ID",
        } for e in row["entities"]["derived_entities"]],
        "quotes": [{
            "passage_id": p["id"], "locator": f"{p['alias']} / {p['role'].upper()} / {p['locator']}",
            "quote": p["text"], "fields": ["原始输入上下文，不表示每个字段均获支持"],
            "start": 0, "end": len(p["text"]), "version_id": p["version_id"],
            "source_sha256": p["source_sha256"], "parser_version": p["parser_version"],
            "coordinate_space": "parsed_passage_unicode_codepoints",
        } for p in packet["passages"]],
        "article_provenance": packet["article"],
        "mapping": {"status": "not_run", "blockers": [
            "独立阶段回归研究，未将本轮新类型视图传给本轮关系模型。",
            "标准ID准确率、未见论文质量与生产导入未在本轮验证。",
        ], "preview": details},
    }


def build(root, conclusions_path):
    root, conclusions_path = Path(root), Path(conclusions_path)
    result, conclusions = read(root / "evaluation.json"), read(conclusions_path)
    require(result["denominator"] == 15, "Expected all 15 groups")
    require(result["timing"]["summary_calls"] == 0, "No summary calls allowed")
    for key in ("findings", "next_steps"):
        require(isinstance(conclusions[key], list) and all(isinstance(s, str) and s for s in conclusions[key]), "Invalid conclusions")
    for name, expected in result["sources"].items():
        path = Path(name)
        require(sha(path) == expected, f"Evaluated source changed: {path}")
    rows = {r["id"]: r for r in result["records"]}
    packets = read(root / "packets.json")["records"]
    require(len(rows) == len(packets) == 15 and set(rows) == {p["id"] for p in packets}, "Card IDs differ")
    methods = []
    for arm, label in ARMS.items():
        entity, relation = (result["scores"][stage][arm] for stage in ("entities", "relations"))
        timing = result["timing"]["relations"][arm]
        methods.append({
            "id": arm, "label": label, "version": result["version"],
            "metrics": [
                ratio("实体核心可用（既有脚本＋本臂规则）", entity["core_usable"]),
                ratio("实体模型原始输出核心可用", entity["raw_core_usable"], note="复用相同010输出；脚本改类型不算模型能力提升。"),
                ratio("关系内容核心通过", relation["core_usable"]),
                ratio("关系结构、原文与内容同时通过", relation["diagnostic_core_ready"]),
                {"label": "关系推理总耗时", "value": round(timing["inference_seconds"], 2), "unit": "秒"},
                {"label": "关系输出tokens", "value": timing["output_tokens"], "unit": "tokens"},
                {"label": "关系进程显存峰值", "value": timing["peak_process_mib"], "unit": "MiB"},
            ],
            "conclusion": "实体零新模型调用；关系每组一次9B。原始输入、原文和字段结构相同，评分口径不变。",
        })
    types = read(root / "entity-types.json")
    methods.append({
        "id": "entity-source-rules", "label": "实体规则具体做了什么", "version": types["version"],
        "metrics": [
            {"label": "检查已有实体", "value": types["counts"]["entities_checked"], "unit": "个"},
            {"label": "有原文依据的类型修正", "value": types["counts"]["changed"], "unit": "个"},
            {"label": "完全不变", "value": types["counts"]["untouched"], "unit": "个"},
            {"label": "脚本总耗时", "value": round(types["script_seconds"], 4), "unit": "秒"},
            {"label": "新增实体模型调用", "value": types["model_calls"], "unit": "次"},
        ],
        "conclusion": "规则1：同篇同版本明确全称(缩写)，首字母严格匹配，定义以inhibition/blockade/therapy/treatment等结束且当前片段有治疗用法，才将无明确药物链接的缩写归为治疗。规则2：仅完整的氨基酸替换＋mutant/mutation短语可由biomarker归为variant；表达签名等不套用。不改原词或推断DNA/标准ID。",
    })
    decision = result["decision"]["relation_prompt"]
    methods.append({
        "id": "adoption", "label": "实际得失与流程取舍", "version": result["version"],
        "metrics": [
            {"label": "新关系提示修复原失败", "value": len(decision["repaired_ids"]), "unit": "组", "note": "、".join(decision["repaired_ids"]) or "无"},
            {"label": "原先通过组退化", "value": len(decision["regressed_ids"]), "unit": "组", "note": "、".join(decision["regressed_ids"]) or "无"},
            {"label": "新关系提示作为默认", "value": "采用" if decision["adoptable"] else "不采用"},
            {"label": "新增概括调用", "value": 0, "unit": "次"},
            {"label": "新增关系任务调用", "value": result["timing"]["new_model_calls"], "unit": "次"},
        ], "conclusion": conclusions["summary"],
    })
    artifacts = ["evaluation.json", "review.json", "reference.json", "failure-audit.json", "entity-types.json", "definition.json", "inputs.json", "packets.json", "plan.json", "preflight.json"]
    provenance = [{"label": name, "artifact": f"{RID}/{name}", "sha256": sha(root / name)} for name in artifacts]
    for name in (DOC, "scripts/source_refinement_pilot.py", "scripts/evaluate_source_refinement.py", "scripts/publish_source_refinement.py", "apps/curation/source_entity_types.py"):
        provenance.append({"label": name, "artifact": name, "sha256": sha(PROJECT / name)})
    provenance.append({"label": "结论", "artifact": str(conclusions_path), "sha256": sha(conclusions_path)})
    prior = PROJECT / "apps/curation/research_data" / f"{RID}.json"
    return prepare_record({
        "schema_version": "research-history-v1", "id": RID, "title": "016 · 直接原文抽取的剩余错误与简单调整",
        "date": read_record(RID)["date"] if prior.exists() else "2026-10-30",
        "stage": "entities", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 15, "sample_unit": "片段组", "summary": conclusions["summary"],
        "task": {
            "question": "无概括9B流程中剩余的实体类型、范围、时间和结论限定错误，能否通过简单脚本或同次抽取约束改善？",
            "input": "同5篇15组。实体重放010输出，规则可查同篇同版本缩写定义；关系原文及实体输入与014完全相同。",
            "outputs": ["原失败字段与英文依据", "实体规则变化及来源", "15组关系配对、退化、耗时", "可采用与不采用的流程调整"],
            "not_measured": ["未见论文泛化", "新增实体串联关系的端到端结果", "全量标准ID正确率", "生产导入与人工审核效率"],
        },
        "dataset": {"label": "同5篇15组开发回归集", "document_count": 5, "role": "开发回归集",
                    "notes": ["不仅复跑原失败组，也保留全部原通过组检查退化。", "新关系模型不读取修正后的实体类型；两项调整独立评分。", "原结果、英文原文、文章与段落版本保留。"]},
        "reference": {"label": "冻结Astra参考与原文核验", "status": "model_reference_not_human_gold", "note": "参考与旧评分先冻结；Astra是模型参考，不是人工金标准。"},
        "methods": methods, "findings": conclusions["findings"], "next_steps": conclusions["next_steps"],
        "identity": {"task_version": result["version"], "completeness": "frozen",
                     "sample_versions": [sha(root / "packets.json"), sha(root / "inputs.json")],
                     "method_versions": [sha(root / name) for name in ("definition.json", "entity-types.json", "evaluation.json")] + [sha(conclusions_path)],
                     "reference_versions": [sha(root / "reference.json"), sha(root / "review.json")]},
        "provenance": provenance, "knowledge_candidates": [card(p, rows[p["id"]]) for p in packets],
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("var/experiments") / RID)
    parser.add_argument("--conclusions", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(register_record(build(args.root, args.conclusions)), ensure_ascii=False))
