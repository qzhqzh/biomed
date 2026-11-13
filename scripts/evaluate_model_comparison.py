#!/usr/bin/env python3
"""Score completed local runs and append a public comparison without GPU work."""

import argparse
from datetime import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.model_comparison import evaluate_runs
from apps.curation.research_history import register_record
from apps.curation.three_class_evaluation import file_hash
from scripts.classify_literature import atomic_json
from scripts.evaluate_three_class import fraction, value

BASE = ROOT / "var/experiments/three-class-20261022"
LABELS = {"solid": "实体瘤", "hematologic": "血液肿瘤", "non_neoplastic": "非肿瘤"}


def public_record(evaluation, args):
    base = args.base
    sample_count = evaluation["sample_count"]
    denominator = evaluation["denominator"]
    unscored = evaluation["unscored"]
    class_counts = "、".join(f"{LABELS[key]}{count}" for key, count in evaluation["reference_by_class"].items())
    groups = evaluation.get("groups", {})
    reference_note = f"{denominator}篇确定参考计分，另{unscored}篇保留展示；模型参考不是人工金标准。"

    methods = []
    for result in evaluation["methods"]:
        metrics = []
        if result["denominator"]:
            metrics.append(fraction("三分类与参考一致", result["matched"], result["denominator"],
                f"{result['denominator']}篇确定参考；错误、拒答和协议失败都保留在分母。"))
        if result["macro_recall"] is not None:
            metrics.append(value("三类召回率均值", round(result["macro_recall"] * 100, 1),
                f"三类等权；本集确定参考：{class_counts}，不能据此推断全库。", "%"))
        metrics.append(value("宏平均 F1", round(result["macro_f1"] * 100, 1), "每类F1等权，含失败导致的漏判；参考未决样本排除。", "%"))
        metrics.extend(fraction(f"{LABELS[category]}识别", item["matched"], item["denominator"], "该参考类别内的召回，非预测精确率。")
            for category, item in result["by_class"].items() if item["denominator"])
        for group in groups.values():
            group_result = next(item for item in group["methods"] if item["id"] == result["id"])
            if group_result["denominator"]:
                metrics.append(fraction(f"{group['label']}与参考一致", group_result["matched"], group_result["denominator"],
                    "分组计分；错误、拒答、协议失败保留在该组确定参考分母。"))
        metrics.extend([
            value("平均处理耗时", round(result["mean_seconds"], 3), "预热后单篇分词、推理和解析；不含模型装载、下载和写盘。", "秒/篇"),
            value("进程显存采样峰值", round(result["nvml_process_peak_mib"] / 1024, 2), "NVML每0.1秒采样，包含运行时与缓存；单次运行，可能漏掉更短峰值。", "GiB"),
            value("PyTorch分配峰值", round(result["peak_allocated_mib"] / 1024, 2), "分配器记录的峰值，包含加载和预热；不等于全部进程占用。", "GiB"),
            value("模型装载耗时", result["timing"]["load_seconds"], "单独记录，不计入单篇处理耗时。", "秒"),
            value("输出失败", result["failed"], "确定参考内协议失败；原始输出保留。"),
            value("信息不足而未完成", result["abstained"], "模型返回null，保留为未完成；在确定参考分母内不计为正确。"),
            value("肿瘤误筛为非肿瘤", result["tumor_false_exclusions"], "参考为实体瘤或血液肿瘤，却判为非肿瘤。")])
        methods.append({"id": result["id"], "label": result["label"], "version": result["revision"],
            "metrics": metrics, "conclusion": "同一冻结开发集的零样本结果；没有微调，也没有用新输出修订参考答案。"})
    notes = [
        f"统一{sample_count}篇：Astra参考明确{denominator}篇（{class_counts}），另{unscored}篇单列；模型参考不是人工金标准。",
        f"同一GPU 3、单卡FP16、batch=1、2个CPU线程；Qwen关闭思考，贪心生成。每个模型预热1次，再测量{sample_count}篇。",
        "输入为同一标题、摘要、MeSH和出版类型，不读全文、不截断；GLiNER使用类别说明接口，Qwen使用冻结提示词，接口差异属于比较条件。",
        "GLiNER单标签取最高分，尚无校准后的拒答阈值；Qwen可返回null，未完成及协议失败在确定参考分母中计错。",
        "速度是各模型在本机兼容实现上的单次测量，不代表最佳服务吞吐；没有量化、训练、批处理优化或全库推理。",
        "参数量由实际载入模型统计，Qwen3.5包含未参与本次文本推理的视觉模块；不能把跨架构或跨代际提升全部归因于参数量。",
        (f"{denominator}篇确定参考中多判对1篇即增加约{100 / denominator:.2f}个百分点；同系列比较也不是仅改变参数数量的严格因果实验。"
            if denominator else "本集暂无确定参考，不报告分类准确率。"),
        "未统一测量系统内存峰值。装载也会占用主机资源，空闲GPU不代表其他服务不受影响。",
    ]
    if groups:
        notes.append(f"{groups['original']['label']}沿用此前已观察过的材料与参考；{groups['added']['label']}单列计分，避免总体成绩掩盖新样本表现；尚不是人工审核的生产验收集。")
    extras = json.loads(args.notes.read_text()) if args.notes else {"findings": [], "next_steps": []}
    provenance = [{"label": label, "artifact": str((base / name).resolve().relative_to(ROOT)), "sha256": file_hash(base / name)}
        for label, name in (("冻结输入", "inputs.json"), ("冻结三分类定义", "task-definition-v1.json"), ("冻结Astra参考", "astra-reference-v1.json"))]
    if args.original_base:
        provenance.extend({"label": f"原集合 / {name}", "artifact": str((args.original_base / name).resolve().relative_to(ROOT)),
            "sha256": file_hash(args.original_base / name)} for name in ("inputs.json", "astra-reference-v1.json"))
    for run in args.runs:
        for name in ("manifest.json", "summary.json"):
            provenance.append({"label": f"{run.name} / {name}", "artifact": str((run/name).resolve().relative_to(ROOT)), "sha256": file_hash(run/name)})
    return {"schema_version": "research-history-v1", "id": args.id, "date": args.date,
        "title": args.title, "summary": "；".join(f"{m['label']} {m['matched']}/{m['denominator']}" for m in evaluation["methods"]),
        "stage": "classification", "status": "evaluated", "sample_count": evaluation["sample_count"], "task_kind": "three_class",
        "classification_definition": {"categories": LABELS, "boundary_notes": evaluation["definition"]["boundary_notes"]},
        "task": {"question": "同一批文献的实体瘤、血液肿瘤、非肿瘤三分类：模型变大是否改善判断？",
            "input": "冻结的标题、摘要、MeSH、出版类型；全部模型得到同一元数据。", "outputs": list(LABELS.values()),
            "not_measured": ["全库与独立留出集效果", "微调和量化收益", "全文证据、实体及关系抽取效果"]},
        "dataset": {"label": f"统一{sample_count}篇开发测试集", "role": "development", "notes": notes[:1] + [notes[-1] if groups else "所有模型沿用同一冻结材料与参考，不为新模型修改样本或答案。"]},
        "reference": {"label": "冻结Astra独立参考 v1", "status": "model_reference_not_human_gold", "note": reference_note},
        "methods": methods, "comparison": {"methods": [{k: v for k, v in m.items() if k != "timing"} for m in evaluation["methods"]],
            "cases": evaluation["cases"], "pairwise": evaluation["pairwise"], "groups": groups, "notes": notes}, "findings": extras["findings"], "next_steps": extras["next_steps"],
        "identity": {"task_version": evaluation["definition"]["task_version"], "sample_versions": [evaluation["input_sha256"]],
            "method_versions": [m["config_sha256"] for m in evaluation["methods"]], "reference_versions": [evaluation["reference_sha256"]], "completeness": "frozen"},
        "provenance": provenance}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--base", type=Path, default=BASE, help="Directory containing frozen inputs, definition and Astra reference")
    parser.add_argument("--sample-count", type=int, default=30, help="Expected number of frozen papers")
    parser.add_argument("--original-base", type=Path, help="Original frozen cohort; verify unchanged and report original/new scores separately")
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(), help="Research date (YYYY-MM-DD; defaults to today in Asia/Shanghai)")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    if args.sample_count < 1:
        parser.error("--sample-count must be positive")
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    evaluation = evaluate_runs(args.base, args.runs, sample_count=args.sample_count, original_base=args.original_base)
    atomic_json(args.output, evaluation)
    if args.register:
        if not args.notes:
            parser.error("Reviewed findings and next steps are required before registration")
        print(json.dumps(register_record(public_record(evaluation, args)), ensure_ascii=False))
    print(json.dumps([{k: m[k] for k in ("label", "matched", "denominator", "macro_recall", "mean_seconds", "nvml_process_peak_mib")} for m in evaluation["methods"]], ensure_ascii=False))
