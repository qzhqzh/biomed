"""Project the measured 019 paired comparison into the existing research page."""

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
from scripts.evaluate_bonsai_relations import ARMS, ROOT, VERSION
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import read, sha
from scripts.publish_summary_ablation import ratio

RID = "bonsai-relations-20261030"
DOC = "docs/research/019-bonsai-relations.md"
LABELS = {"qwen9b": "Qwen3.5-9B（复用014）", "bonsai27b": "Bonsai 2 27B（本轮）"}


def arm_method(evaluation, arm):
    score, timing = evaluation["scores"][arm], evaluation["timing"]["arms"][arm]
    metrics = [
        ratio(label, score[key], note=note)
        for key, label, note in (
            (
                "complete_task_ready",
                "完整任务通过",
                "相同014脚本诊断结构通过、语义核心可用、且聚焦ANCHOR；两模型本轮同标复核。",
            ),
            (
                "structured_common_core_usable",
                "语义核心可用",
                "Astra依据英文原文核验；不是人工金标准。",
            ),
            (
                "strict_schema_valid",
                "严格结构通过",
                "原始输出满足无statement结构，尚未应用无损格式适配。",
            ),
            (
                "diagnostic_schema_valid",
                "相同脚本诊断后结构通过",
                "两臂均只用014既有格式适配、原词大小写定位、附加实体ID处理；不修医学语义。",
            ),
            (
                "diagnostic_core_ready",
                "诊断后核心就绪",
                "结构和语义均通过，另以完整任务指标检查是否聚焦ANCHOR。",
            ),
        )
    ]
    axis = score["axes"]["numeric_condition_scope_binding"]
    metrics.append(
        ratio(
            "数值、条件与场景绑定",
            axis["pass"],
            axis["applicable_denominator"],
            f"不适用{axis['not_applicable']}组。",
        )
    )
    metrics.extend(
        [
            {
                "label": "推理总耗时",
                "value": round(timing["inference_seconds"], 2),
                "unit": "秒",
                "note": "同15组，单次实测；不含加载、预热、参考审核。运行库和精度不同，不能仅归因为参数量。",
            },
            {
                "label": "平均每组耗时",
                "value": round(timing["mean_seconds"], 2),
                "unit": "秒",
            },
            {
                "label": "进程显存峰值",
                "value": timing["peak_process_mib"]
                if timing["peak_process_mib"] is not None
                else "未记录",
                "unit": "MiB",
                "note": "NVML进程实测峰值，包含推理开销，不等于权重文件体积。",
            },
            {
                "label": "输出tokens",
                "value": timing["output_tokens"],
                "unit": "tokens",
                "note": "各模型原生tokenizer；跨模型不能视为相同计量单位。",
            },
            {
                "label": "GPU",
                "value": (timing.get("hardware") or {}).get("name", "未记录"),
            },
            {
                "label": "精度/格式",
                "value": timing.get("precision") or "见实验manifest",
            },
        ]
    )
    return {
        "id": arm,
        "label": LABELS[arm],
        "version": evaluation["version"],
        "metrics": metrics,
        "conclusion": "同5篇15组开发样本、同英文提示内容；比较实际部署，不是独立临床准确率。",
    }


def card(packet, row):
    require(row["original_passages"] == packet["passages"], "Original passages changed")
    anchors = [p for p in packet["passages"] if p["role"] == "anchor"]
    require(len(anchors) == 1 and anchors[0]["text"], "Need one original ANCHOR")
    passage_texts = {p["id"]: p["text"] for p in packet["passages"]}
    selected = set()
    for arm in ARMS:
        fact = row["arms"][arm]["raw_fact"]
        if isinstance(fact, dict) and isinstance(fact.get("entity_ids"), list):
            selected.update(eid for eid in fact["entity_ids"] if isinstance(eid, str))
    entities = []
    for entity in packet["entities"]:
        if entity["id"] not in selected:
            continue
        candidate = (entity.get("link") or {}).get("candidate_id")
        entities.append(
            {
                "id": entity["id"],
                "type": entity["type"],
                "text": entity["text"],
                "normalization_status": "两臂所选ID并集；既有标准候选未确认",
                "standard_ids": [candidate] if candidate else [],
                "source_anchored": _located(entity, passage_texts),
            }
        )
    notes = []
    for arm in ARMS:
        result = row["arms"][arm]
        notes.append(
            f"{LABELS[arm]}：完整任务{'通过' if result['complete_task_ready'] else '未通过'}；{result['review']['brief']}"
        )
    return {
        "id": packet["id"],
        "pmid": packet["article"]["pmid"],
        "title": packet["article"]["title"],
        "statement": anchors[0]["text"],
        "status": "supported"
        if row["arms"]["bonsai27b"]["complete_task_ready"]
        else "needs_revision",
        "review_note": " ".join(notes),
        "study_scope": "卡片主文是英文ANCHOR；两臂事实、范围及审核见展开字段。这里只展示研究对照，不发布知识。",
        "source_role": "原段是冻结研究输入；模型实际引用见各臂evidence和result.passage字段，展示全部段落不等于全部支持每个事实。",
        "entities": entities,
        "quotes": [
            {
                "passage_id": p["id"],
                "locator": f"{p['alias']} / {p['role'].upper()} / {p['locator']}",
                "quote": p["text"],
                "fields": ["原始输入上下文，不等于逐字段证据认定"],
                "start": 0,
                "end": len(p["text"]),
                "version_id": p["version_id"],
                "source_sha256": p["source_sha256"],
                "parser_version": p["parser_version"],
                "coordinate_space": "parsed_passage_unicode_codepoints",
            }
            for p in packet["passages"]
        ],
        "article_provenance": packet["article"],
        "mapping": {
            "status": "not_run",
            "blockers": [
                "本轮是模型研究对照，没有导入或修订已发布知识，没有修改tumor_api。",
                "既有标准ID候选未确认；研究通过不等于完成生产临床适用验证。",
            ],
            "preview": {
                "Qwen9B原始事实": row["arms"]["qwen9b"]["raw_fact"],
                "Bonsai原始事实": row["arms"]["bonsai27b"]["raw_fact"],
                "本轮同标准独立审核": {
                    LABELS[arm]: row["arms"][arm]["review"] for arm in ARMS
                },
                "相同脚本结构诊断": {
                    LABELS[arm]: row["arms"][arm]["mechanical"] for arm in ARMS
                },
                "错误类别变化": row["error_changes"],
            },
        },
    }


def build(root, conclusions_path):
    root, conclusions_path = Path(root), Path(conclusions_path)
    evaluation, conclusions = read(root / "evaluation.json"), read(conclusions_path)
    require(
        evaluation["version"] == VERSION
        and evaluation["denominator"] == 15
        and evaluation["state"] == "research_comparison_not_knowledge_export",
        "Expected completed 019 comparison",
    )
    require(
        isinstance(conclusions.get("summary"), str) and conclusions["summary"].strip(),
        "Supply measured summary",
    )
    for key in ("findings", "next_steps"):
        require(
            isinstance(conclusions.get(key), list)
            and all(
                isinstance(value, str) and value.strip() for value in conclusions[key]
            ),
            f"Supply conclusions {key}",
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
        == evaluation["scores"]["bonsai27b"]["complete_task_ready"],
        "Card total differs",
    )
    methods = [arm_method(evaluation, arm) for arm in ARMS]
    pairs = evaluation["paired"]["complete_task_ready"]
    methods.append(
        {
            "id": "paired-complete-task",
            "label": "完整任务通过：同组对照",
            "version": VERSION,
            "metrics": [
                ratio(
                    label,
                    pairs[key]["count"],
                    note="组别：" + ("、".join(pairs[key]["ids"]) or "无"),
                )
                for key, label in (
                    ("both_pass", "两臂均通过"),
                    ("qwen9b_only", "仅9B通过"),
                    ("bonsai27b_only", "仅Bonsai通过"),
                    ("both_fail", "两臂均未通过"),
                )
            ],
            "conclusion": "旧9B原始结果和新Bonsai结果均按本轮同一标准重新审核；不是直接对照旧014或018分数。",
        }
    )
    artifact_names = (
        "evaluation.json",
        "reference.json",
        "review.json",
        "definition.json",
        "inputs.json",
        "packets.json",
        "plan.json",
        "extract/manifest.json",
        "extract/summary.json",
    )
    provenance = [
        {"label": name, "artifact": f"{RID}/{name}", "sha256": sha(root / name)}
        for name in artifact_names
    ]
    provenance += [
        {"label": name, "artifact": name, "sha256": sha(PROJECT / name)}
        for name in (
            DOC,
            "scripts/evaluate_bonsai_relations.py",
            "scripts/publish_bonsai_relations.py",
        )
    ]
    provenance.append(
        {
            "label": "实测结论",
            "artifact": f"{RID}/{conclusions_path.name}",
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
            "title": "019 · Bonsai 2与9B关系抽取对照",
            "date": date,
            "stage": "entities",
            "status": "evaluated",
            "task_kind": "knowledge_candidates",
            "sample_count": 15,
            "sample_unit": "片段组",
            "summary": conclusions["summary"],
            "task": {
                "question": "同一批有效英文片段与实体提示，Bonsai 2能否提高关系、条件及数值绑定的完整通过率，耗时和显存代价如何？",
                "input": "逐字复用014的无statement提示、原文与实体输入，9B已有原始结果不重跑。",
                "outputs": [
                    "同标双臂审核与完整任务通过率",
                    "每组错误和配对通过结果",
                    "本机部署耗时、token量与显存",
                    "文章和原始段落两层出处",
                ],
                "not_measured": [
                    "独立临床准确率或全库效果",
                    "单独由参数量变化引起的因果效果",
                    "训练或微调效果",
                    "生产知识导入",
                ],
            },
            "dataset": {
                "label": "同5篇15组开发回归集",
                "document_count": 5,
                "role": "开发回归集",
                "notes": [
                    "复用014的15次9B结果，本轮只新做15次Bonsai任务推理。",
                    "冻结文件完全相同，保留旧K07输入；不使用后来重新准备的工作流输入。",
                    "两臂各一次；模型量化格式、运行库和原生tokenizer不同。",
                ],
            },
            "reference": {
                "label": "Astra本轮同标准原文参考审核",
                "status": "model_reference_not_human_gold",
                "note": "旧9B输出也重新核验，不继承018发布比例；不是人工金标准。",
            },
            "methods": methods,
            "findings": conclusions["findings"],
            "next_steps": conclusions["next_steps"],
            "identity": {
                "task_version": VERSION,
                "completeness": "frozen",
                "sample_versions": [
                    sha(root / name) for name in ("inputs.json", "packets.json")
                ],
                "method_versions": [
                    sha(root / name)
                    for name in (
                        "evaluation.json",
                        "definition.json",
                        "extract/manifest.json",
                    )
                ]
                + [sha(conclusions_path)],
                "reference_versions": [
                    sha(root / name) for name in ("reference.json", "review.json")
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
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print the projection without registration",
    )
    args = parser.parse_args()
    record = build(args.root, args.conclusions)
    print(
        json.dumps(
            record if args.preview else register_record(record),
            ensure_ascii=False,
            indent=2,
        )
    )
