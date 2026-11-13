"""Register the frozen paired summary study with supplied, measured conclusions."""

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

RID = "summary-ablation-20261029"
ROOT = PROJECT / "var/experiments" / RID
DOC = "docs/research/014-summary-ablation.md"
ARMS = {"with_summary": "有概括（复用012）", "without_summary": "无概括（本轮）"}


def ratio(label, count, denominator=15, note=""):
    if denominator == 0:
        return {"label": label, "value": "不适用", "note": note}
    return {
        "label": label,
        "numerator": count,
        "denominator": denominator,
        "note": note,
    }


def arm_method(evaluation, arm):
    scores = evaluation["scores"][arm]
    timing = evaluation["timing"]["arms"][arm]
    metrics = [
        ratio(label, scores[key], note=note)
        for label, key, note in (
            (
                "共同字段核心可用",
                "structured_common_core_usable",
                "Astra依据英文原文评共同字段，不使用概括证明这些字段。",
            ),
            (
                "核心可用且聚焦ANCHOR",
                "core_and_anchor",
                "核心可用并且完成目标段落任务。",
            ),
            (
                "严格结构通过",
                "strict_schema_valid",
                "原始输出满足共同字段结构，无格式包装。",
            ),
            (
                "诊断后核心就绪",
                "diagnostic_core_ready",
                "同样的格式、名称大小写、附加ID诊断后，结构与核心语义均通过。",
            ),
        )
    ]
    for axis, label in (
        ("drug_identity_roles", "药物身份与角色"),
        ("type_purity", "实体类型纯度"),
        ("numeric_condition_scope_binding", "数值、条件与场景绑定"),
    ):
        item = scores["axes"][axis]
        metrics.append(
            ratio(
                label,
                item["pass"],
                item["applicable_denominator"],
                f"不适用 {item['not_applicable']} 组；不适用项不计为失败。",
            )
        )
    metrics.extend(
        [
            {
                "label": "输出tokens",
                "value": timing["output_tokens"],
                "unit": "tokens",
                "note": "同15组实测总量。",
            },
            {
                "label": "推理总耗时",
                "value": round(timing["inference_seconds"], 2),
                "unit": "秒",
                "note": "不含载入、预热、Astra审核；每臂一次实测，不能外推全库。",
            },
            {
                "label": "进程显存峰值",
                "value": timing["peak_process_mib"]
                if timing["peak_process_mib"] is not None
                else "未记录",
                "unit": "MiB",
                "note": "NVML进程峰值；不等于模型权重大小。",
            },
        ]
    )
    return {
        "id": arm,
        "label": ARMS[arm],
        "version": evaluation["version"],
        "metrics": metrics,
        "conclusion": "同5篇15组开发样本；共同字段审核与概括本身诊断分开。",
    }


def card(packet, row):
    old, new = row["arms"]["with_summary"], row["arms"]["without_summary"]
    anchor = [p for p in packet["passages"] if p["role"] == "anchor"]
    require(len(anchor) == 1 and bool(anchor[0]["text"]), "Need one original ANCHOR")
    selected = set((old["raw_fact"] or {}).get("entity_ids", []))
    passages = {p["id"]: p["text"] for p in packet["passages"]}
    entities = []
    for entity in packet["entities"]:
        if entity["id"] not in selected:
            continue
        candidate_id = (entity.get("link") or {}).get("candidate_id")
        entities.append(
            {
                "id": entity["id"],
                "type": entity["type"],
                "text": entity["text"],
                "normalization_status": "有概括臂原选词；标准候选未确认"
                if candidate_id
                else "有概括臂原选词；无标准关联",
                "standard_ids": [candidate_id] if candidate_id else [],
                "source_anchored": _located(entity, passages),
            }
        )
    notes = [
        "主文为英文原文，双臂输出在展开字段。实体表是有概括臂原选词及输入提示，不代表无概括臂生成了概括。"
    ]
    for arm, result in row["arms"].items():
        review = result["review"]
        notes.append(
            f"{ARMS[arm]}：共同字段核心{'可用' if review['structured_common_core_usable'] else '不可用'}，"
            f"技术诊断{'通过' if result['diagnostic_schema_valid'] else '未通过'}；{review['brief']}"
        )
    quotes = [
        {
            "passage_id": p["id"],
            "locator": f"{p['alias']} / {p['role'].upper()} / 研究输入上下文 · {p['locator']}",
            "quote": p["text"],
            "fields": ["研究原文上下文，非模型字段支持或引用认定"],
            "start": 0,
            "end": len(p["text"]),
            "version_id": p["version_id"],
            "source_sha256": p["source_sha256"],
            "parser_version": p["parser_version"],
            "coordinate_space": "parsed_passage_unicode_codepoints",
        }
        for p in packet["passages"]
    ]
    ready = (
        new["review"]["structured_common_core_usable"]
        and new["diagnostic_schema_valid"]
    )
    require(
        ready == new["diagnostic_core_ready"], "Card readiness differs from evaluation"
    )
    return {
        "id": packet["id"],
        "pmid": packet["article"]["pmid"],
        "title": packet["article"]["title"],
        "statement": anchor[0]["text"],
        "status": "supported" if ready else "needs_revision",
        "review_note": " ".join(notes),
        "study_scope": "见两臂展开字段；卡片主文是原始ANCHOR，不是模型概括。",
        "source_role": "全部英文原段为研究输入上下文；具体模型引用见各臂原始evidence字段。",
        "entities": entities,
        "quotes": quotes,
        "article_provenance": packet["article"],
        "mapping": {
            "status": "not_run",
            "blockers": [
                "本轮是双臂研究比较，不导出知识，不是tumor_api导入预览。",
                "标准ID仅为既有候选；所有段落展示不等于模型已引用或逐字段支持。",
            ],
            "preview": {
                "有概括臂原始事实": old["raw_fact"],
                "无概括臂原始事实": new["raw_fact"],
                "有概括臂共同字段审核": old["review"],
                "无概括臂共同字段审核": new["review"],
                "两臂结构及脚本诊断": {
                    arm: result["mechanical"] for arm, result in row["arms"].items()
                },
                "旧概括自身诊断（非因果结论）": row["baseline_statement_diagnostic"],
                "错误类别变化": row["error_changes"],
            },
        },
    }


def build(root, conclusions_path):
    root, conclusions_path = Path(root), Path(conclusions_path)
    evaluation, conclusions = read(root / "evaluation.json"), read(conclusions_path)
    require(
        evaluation["denominator"] == 15
        and evaluation["state"] == "research_comparison_not_knowledge_export",
        "Expected completed 15-group comparison",
    )
    require(
        isinstance(conclusions.get("summary"), str)
        and bool(conclusions["summary"].strip()),
        "Supply measured summary",
    )
    for field in ("findings", "next_steps"):
        require(
            isinstance(conclusions.get(field), list)
            and all(isinstance(x, str) and x.strip() for x in conclusions[field]),
            f"Supply conclusions {field}",
        )
    for source in evaluation["sources"]:
        path = Path(source["path"])
        require(
            sha(path if path.is_absolute() else PROJECT / path) == source["sha256"],
            "Evaluated source changed",
        )
    packets = read(root / "packets.json")["records"]
    rows = indexed(evaluation["records"], "evaluation")
    require(
        len(packets) == len(rows) == 15 and {p["id"] for p in packets} == set(rows),
        "Paired card IDs differ",
    )
    cards = [card(packet, rows[packet["id"]]) for packet in packets]
    require(
        sum(c["status"] == "supported" for c in cards)
        == evaluation["scores"]["without_summary"]["diagnostic_core_ready"],
        "Card total differs from evaluation",
    )
    pairs = evaluation["paired"]["common_core"]
    methods = [arm_method(evaluation, arm) for arm in ARMS]
    methods.append(
        {
            "id": "paired-common-core",
            "label": "共同字段核心可用：配对四格",
            "version": evaluation["version"],
            "metrics": [
                ratio(
                    label,
                    pairs[key]["count"],
                    note="组别：" + ("、".join(pairs[key]["ids"]) or "无"),
                )
                for key, label in (
                    ("both_pass", "两臂均可用"),
                    ("with_summary_only", "仅有概括可用"),
                    ("without_summary_only", "仅无概括可用"),
                    ("both_fail", "两臂均不可用"),
                )
            ],
            "conclusion": "同组对照，只反映本批样本，不宣称全库效果。",
        }
    )
    methods.append(
        {
            "id": "baseline-statement-diagnostic",
            "label": "旧概括自身诊断",
            "version": evaluation["version"],
            "metrics": [
                ratio(
                    "概括本身有原文支持",
                    evaluation["baseline_statement"]["supported"],
                    note="仅为15条概括自身诊断；不证明概括帮助或导致共同字段错误。",
                ),
                {
                    "label": "概括与共同字段冲突组数",
                    "value": len(evaluation["baseline_statement"]["conflict_ids"]),
                    "unit": "组",
                },
            ],
            "conclusion": "概括自身有据与结构字段正确是两项指标；冲突本身不是因果证据。",
        }
    )
    artifacts = [
        "evaluation.json",
        "reference.json",
        "review.json",
        "definition.json",
        "plan.json",
        "inputs.json",
        "packets.json",
        "executed-code.json",
    ]
    provenance = [
        {"label": name, "artifact": f"{RID}/{name}", "sha256": sha(root / name)}
        for name in artifacts
    ]
    for name in (
        DOC,
        "scripts/publish_summary_ablation.py",
        "scripts/evaluate_summary_ablation.py",
        "scripts/summary_ablation_pilot.py",
    ):
        provenance.append(
            {"label": name, "artifact": name, "sha256": sha(PROJECT / name)}
        )
    provenance.append(
        {
            "label": "实测结论",
            "artifact": str(conclusions_path),
            "sha256": sha(conclusions_path),
        }
    )
    prior = PROJECT / "apps/curation/research_data" / f"{RID}.json"
    date = (
        read_record(RID)["date"]
        if prior.exists()
        else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    )
    return prepare_record(
        {
            "schema_version": "research-history-v1",
            "id": RID,
            "title": "014 · 概括是否帮助结构化抽取",
            "date": date,
            "stage": "entities",
            "status": "evaluated",
            "task_kind": "knowledge_candidates",
            "sample_count": 15,
            "sample_unit": "片段组",
            "summary": conclusions["summary"],
            "task": {
                "question": "同15组、同9B，保留或取消概括字段如何影响共同结构字段的可用性、错误与成本？",
                "input": "相同5篇15组英文原文、实体提示；仅删除statement字段及其一行说明。",
                "outputs": [
                    "共同结构字段双臂审核及配对四格",
                    "错误类型、输出tokens、耗时与显存",
                    "15组英文原文与双臂完整原始输出",
                ],
                "not_measured": [
                    "全库效果与独立临床准确率",
                    "人工审核耗时",
                    "生产知识库导入",
                ],
            },
            "dataset": {
                "label": "同5篇15组开发回归集",
                "document_count": 5,
                "role": "开发回归集",
                "notes": [
                    "有概括臂复用012原始15次结果，无概括臂新做15次；每臂只测一次。",
                    "有概括输出不提供给新9B；卡片主文是原始ANCHOR。",
                ],
            },
            "reference": {
                "label": "Astra冻结共同字段参考与双臂审核",
                "status": "model_reference_not_human_gold",
                "note": "原始英文为依据，概括不作为共同字段的支持证据；不是人工金标准。",
            },
            "methods": methods,
            "findings": conclusions["findings"],
            "next_steps": conclusions["next_steps"],
            "identity": {
                "task_version": evaluation["version"],
                "completeness": "frozen",
                "sample_versions": [
                    sha(root / "inputs.json"),
                    sha(root / "packets.json"),
                ],
                "method_versions": [
                    sha(root / name)
                    for name in (
                        "evaluation.json",
                        "definition.json",
                        "executed-code.json",
                    )
                ]
                + [sha(conclusions_path)],
                "reference_versions": [
                    sha(root / "reference.json"),
                    sha(root / "review.json"),
                ],
            },
            "provenance": provenance,
            "knowledge_candidates": cards,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--conclusions", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            register_record(build(args.root, args.conclusions)), ensure_ascii=False
        )
    )
