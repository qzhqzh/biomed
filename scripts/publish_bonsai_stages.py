"""Project the three frozen Bonsai comparisons into one immutable 020 record."""
# ruff: noqa: E402

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.research_history import (
    DIRECTORY,
    MAX_BYTES,
    prepare_record,
    read_record,
    register_record,
)
from scripts.evaluate_core_knowledge import require
from scripts.evaluate_knowledge_candidates import read, sha
from scripts.publish_summary_ablation import ratio

RID = "bonsai-stages-20261101"
ROOT = PROJECT / "var/experiments" / RID
DOC = "docs/research/020-bonsai-stages.md"
VERSION = "bonsai-stages-publication-v1"
STAGES = ("classification", "passages", "entities")
SCREENING_ARMS = ("qwen35-4b", "qwen35-9b", "bonsai27b")
ENTITY_ARMS = ("qwen9b", "bonsai27b")
LABELS = {
    "qwen35-4b": "Qwen3.5-4B（复用）",
    "qwen35-9b": "Qwen3.5-9B（复用）",
    "qwen9b": "Qwen3.5-9B（复用）",
    "bonsai27b": "Bonsai 2 27B（本轮，PQ2_0）",
}
CATEGORIES = {"solid": "实体瘤", "hematologic": "血液肿瘤", "non_neoplastic": "非肿瘤"}
COMMON_ARTIFACTS = (
    "inputs.json", "definition.json", "source-inputs.json", "plan.json",
    "reference.json", "evaluation.json", "extract/manifest.json", "extract/summary.json",
)
EXTRA_ARTIFACTS = {
    "classification": ("source-definition.json",),
    "passages": ("source-definition.json", "astra-reference-v1.json", "reference-review.json"),
    "entities": (
        "review.json", "legacy-reference.json", "historical-016-reference.json",
        "historical-016-review.json", "source-context.json", "guard-metadata.json",
        "fulltext-context.json", "assembled.json", "entity-types.json", "common-projections.json",
    ),
}


def value(label, number, unit="", note=""):
    return {"label": label, "value": number, "unit": unit, "note": note}


def percentage(label, number, note=""):
    return value(label, round(number * 100, 2) if number is not None else "不适用", "%", note)


def resources(timing, count, unit):
    require(timing["inference_seconds"] >= 0 and timing["mean_seconds"] >= 0,
            "Missing measured inference time")
    require(timing["peak_process_mib"] is not None and timing["peak_process_mib"] > 0,
            "Missing measured process GPU memory")
    return [
        value("推理总耗时", round(timing["inference_seconds"], 2), "秒",
              f"同{count}{unit}单次实测；不含加载、预热、Astra审核。精度和运行库不同，只比较本机部署。"),
        value(f"平均每{unit}耗时", round(timing["mean_seconds"], 3), "秒"),
        value("进程显存峰值", timing["peak_process_mib"], "MiB", "NVML进程峰值；包含推理开销，不等于模型权重大小。"),
        value("精度/格式", timing.get("precision") or "见冻结manifest"),
        value("GPU", (timing.get("hardware") or {}).get("name", "见冻结summary")),
    ]


def classification_method(evaluation, arm):
    score = evaluation["methods"][arm]
    require(score["total"] == 100 and score["denominator"] == 95 and score["unscored"] == 5,
            "Classification scoring denominator changed")
    metrics = [ratio("三分类准确率", score["matched"], 95,
                     "100篇输入中95篇参考已定；5篇参考未决排除。模型未完成仍计入95篇分母。")]
    for category, label in CATEGORIES.items():
        per_class = score["by_class"][category]
        tp = per_class["matched"]
        fn = per_class["denominator"] - tp
        fp = sum(score["confusion"][other][category] for other in CATEGORIES if other != category)
        metrics.append(ratio(f"{label}召回", tp, per_class["denominator"],
                             f"TP {tp}；FN {fn}；FP {fp}。按该类参考文献计数。"))
    metrics += [
        percentage("三类宏平均F1", score["macro_f1"]),
        value("肿瘤误判为非肿瘤", score["tumor_false_exclusions"], "篇"),
        value("计分样本中未完成", score["abstained"] + score["failed"], "篇",
              "弃答、截断或协议失败不自动当作非肿瘤。"),
        value("参考未决（不计准确率）", score["unscored"], "篇"),
        *resources(score["timing"], 100, "篇"),
    ]
    return {
        "id": f"classification-{arm}", "label": f"文献三分类 · {LABELS[arm]}",
        "version": evaluation["version"], "metrics": metrics,
        "conclusion": "复用100篇开发样本、原提示和冻结Astra参考；该任务只判断实体瘤、血液肿瘤、非肿瘤。",
    }


def passage_method(evaluation, arm):
    score = evaluation["methods"][arm]
    require(score["total"] == score["denominator"] == 282
            and score["positive"] == 122 and score["negative"] == 160,
            "Passage scoring denominator changed")
    tp, fp, fn = (score[key] for key in ("tp", "fp", "fn"))
    metrics = [
        ratio("证据段落召回", tp, 122, "分母为冻结v2参考中的122个正段落；未完成的正段落计入漏检。"),
        ratio("所选段落精确率", tp, tp + fp, "分母为模型选中的段落；不是独立知识条数。"),
        percentage("段落F1", score["f1"]),
        value("正确选中 TP", tp, "段"),
        value("漏检 FN", fn, "段"),
        value("误收 FP", fp, "段"),
        value("未完成输出", score["pending_all"], "段", "未完成的负段落不当作正确排除。"),
        *resources(score["timing"], 282, "段"),
    ]
    return {
        "id": f"passages-{arm}", "label": f"证据段落筛选 · {LABELS[arm]}",
        "version": evaluation["version"], "metrics": metrics,
        "conclusion": "同5篇全文的282个已解析段落，122正、160负；复用原提示与冻结v2参考。全文解析遗漏不在此分母中。",
    }


def entity_method(evaluation, arm):
    score = evaluation["scores"][arm]
    require(score["groups"] == evaluation["denominator"] == 15, "Entity denominator changed")
    required, missing = score["current_required_items"], score["current_missing_items"]
    metrics = [
        ratio("原始实体核心可用", score["raw_core_usable"], 15, "按冻结016当前核心要求核验原始实体抽取。"),
        ratio("共同类别映射后核心可用", score["projected_core_usable"], 15, "两臂应用相同的既有类别映射，不使用Bonsai专属修正。"),
        ratio("相同原文规则后核心可用", score["refined_core_usable"], 15, "两臂均应用016已有原文类型规则，并分别核验修改是否有原文支持。"),
        ratio("必要身份完整", score["current_identity_complete"], 15, "只计冻结参考所需的实体身份；身份完整不等于类型、关系或标准ID全部正确。"),
        ratio("必要实体身份覆盖", required - missing, required, "按片段组所需身份计数；同实体跨组可重复，不是全实体召回率。"),
        value("必要身份遗漏", missing, "项"),
        value("明确类型错误", score["clear_type_issues"], "项", "原输出审核计数；不含家族/粒度边界，不能当作全实体FP。"),
        value("家族或粒度边界", score["family_granularity_boundaries"], "项"),
        ratio("原文精确定位", score["exact_source_mentions"], score["deduplicated_mentions"],
              "分母为每组去重后的输出提及；文字可定位不证明语义或关系正确。"),
        *resources(evaluation["timing"]["arms"][arm], 15, "组"),
    ]
    return {
        "id": f"entities-{arm}", "label": f"实体抽取 · {LABELS[arm]}",
        "version": evaluation["version"], "metrics": metrics,
        "conclusion": "同5篇15组、相同原文与提示；按016冻结口径新审Bonsai，复用旧9B审核。未测全实体Precision/Recall或标准ID准确率。",
    }


def checked_evaluation(folder, stage):
    evaluation, plan = read(folder / "evaluation.json"), read(folder / "plan.json")
    expected = "bonsai-entities-evaluation-v1" if stage == "entities" else "bonsai-screening-pair-v1"
    require(evaluation["version"] == expected and evaluation["stage"] == plan["stage"] == stage,
            f"Unexpected completed {stage} evaluation")
    verified = {}
    for source in [*plan["sources"], *evaluation["sources"]]:
        path = Path(source["path"])
        path = path if path.is_absolute() else PROJECT / path
        require(sha(path) == source["sha256"], f"Evaluated frozen source changed: {path}")
        if source.get("copy"):
            require(sha(folder / source["copy"]) == source["sha256"], "Frozen source copy changed")
        verified[str(path.resolve())] = source["sha256"]
    # A source check must bind the actual displayed run, not merely some old files.
    covered = {str((Path(source["path"]) if Path(source["path"]).is_absolute()
                    else PROJECT / source["path"]).resolve()) for source in evaluation["sources"]}
    for name in COMMON_ARTIFACTS:
        if name != "evaluation.json":
            require(str((folder / name).resolve()) in covered, f"Evaluation does not bind {stage}/{name}")
    ref_key = "reference_sha256" if stage == "entities" else "frozen_reference_sha256"
    require(sha(folder / "reference.json") == plan[ref_key], "Frozen reference changed")
    if stage == "entities":
        review = read(folder / "review.json")
        require(review["status"] == "complete" and review["model"] == "gpt-6-astra"
                and review["reference_sha256"] == sha(folder / "reference.json"), "Incomplete or rebound entity review")
        require(str((folder / "review.json").resolve()) in covered, "Evaluation does not bind entity review")
        require(set(evaluation["scores"]) == set(ENTITY_ARMS), "Wrong entity arms")
    else:
        require(set(evaluation["methods"]) == set(SCREENING_ARMS), "Wrong screening arms")
        require(evaluation["reference_sha256"] == plan[ref_key], "Evaluation reference differs")
    return evaluation, verified


def optional_cascade_artifacts(root):
    path = root / "classification/cascade-replay.json"
    if not path.exists():
        return []
    replay = read(path)
    require(replay.get("status") == "posthoc_development_replay_not_new_inference"
            and type(replay.get("new_model_calls")) is int and replay["new_model_calls"] == 0,
            "Cascade replay must remain offline development analysis without new inference")
    require(replay.get("source_sha256") == sha(root / "classification/evaluation.json"),
            "Cascade replay classification evaluation changed")
    return [("classification", "cascade-replay.json", path)]


def build(root=ROOT, conclusions_path=None):
    root = Path(root).absolute()
    conclusions_path = Path(conclusions_path).absolute() if conclusions_path else root / "conclusions.json"
    evaluations, sources = {}, {}
    for stage in STAGES:
        evaluations[stage], checked = checked_evaluation(root / stage, stage)
        sources.update(checked)
    conclusions = read(conclusions_path)
    require(isinstance(conclusions.get("summary"), str) and conclusions["summary"].strip(),
            "Supply measured summary")
    for key in ("findings", "next_steps"):
        require(isinstance(conclusions.get(key), list) and bool(conclusions[key])
                and all(isinstance(item, str) and item.strip() for item in conclusions[key]),
                f"Supply measured conclusions {key}")
    methods = [classification_method(evaluations["classification"], arm) for arm in SCREENING_ARMS]
    methods += [passage_method(evaluations["passages"], arm) for arm in SCREENING_ARMS]
    methods += [entity_method(evaluations["entities"], arm) for arm in ENTITY_ARMS]

    artifacts = [(stage, name, root / stage / name) for stage in STAGES
                 for name in (*COMMON_ARTIFACTS, *EXTRA_ARTIFACTS[stage])]
    artifacts += optional_cascade_artifacts(root)
    provenance = [{"label": f"{stage}/{name}", "artifact": f"{RID}/{stage}/{name}", "sha256": sha(path)}
                  for stage, name, path in artifacts]
    extra = [(DOC, PROJECT / DOC), ("scripts/publish_bonsai_stages.py", Path(__file__)),
             (f"{RID}/{conclusions_path.name}", conclusions_path)]
    provenance += [{"label": label, "artifact": label, "sha256": sha(path)} for label, path in extra]
    sample_versions, method_versions, reference_versions = [], [], []
    for _, name, path in artifacts:
        target = reference_versions if "reference" in name or "review" in name else (
            sample_versions if name in ("inputs.json", "source-inputs.json", "source-context.json", "fulltext-context.json")
            else method_versions)
        target.append(sha(path))
    method_versions += [sha(path) for _, path in extra]
    # The evaluated sources already preserve every original output and baseline hash.
    # Retain a compact identity for that fully rechecked graph instead of copying raw rows.
    method_versions.append(digest(sources))
    date = (read_record(RID)["date"] if (DIRECTORY / f"{RID}.json").exists()
            else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"))
    record = prepare_record({
        "schema_version": "research-history-v1", "id": RID,
        "title": "020 · Bonsai 2在分类、段落筛选与实体抽取中的对照",
        "date": date, "stage": "entities", "status": "evaluated",
        "task_kind": "multi_stage_comparison", "sample_count": 397, "sample_unit": "任务输入",
        "summary": conclusions["summary"],
        "task": {
            "question": "Bonsai 2在文献三分类、有效段落筛选、实体抽取中能否改善质量，耗时和显存代价如何？",
            "input": "逐字复用各阶段冻结提示和输入；保留旧4B/9B结果，只新运行Bonsai。",
            "outputs": ["三分类准确率与各类召回", "证据段落漏检、误收与F1",
                        "实体原始核心、共同映射后和同规则后核心可用率", "同样本配对得失及本机耗时、显存"],
            "not_measured": ["独立测试集或全库效果", "全实体精确率、标准ID准确率及生产知识质量",
                             "仅由参数量导致的效果变化", "训练或微调效果"],
        },
        "dataset": {
            "label": "100篇三分类；5篇全文的282段筛选；同5篇15个实体组",
            "role": "冻结开发回归集",
            "notes": ["397是100 + 282 + 15个不同任务输入的合计，不是397篇文献。",
                      "三分类参考95篇已定、5篇未决；段落参考122正、160负；实体按15组评分。各任务分母不能混合。",
                      "段落与实体来自同5篇全文；各任务分别复用原输入，不是本轮串联后的端到端质量评测。",
                      "各臂一次测量；量化、运行库和原生tokenizer不同，只比较实际部署结果。"],
        },
        "reference": {
            "label": "冻结Astra参考；实体按016同口径审核",
            "status": "model_reference_not_human_gold",
            "note": "分类复用冻结100篇参考；段落复用冻结v2参考。实体复用016当前核心门槛和旧9B审核，新审Bonsai并核验相同规则修改；Astra是模型参考，不是人工金标准。",
        },
        "methods": methods, "findings": conclusions["findings"], "next_steps": conclusions["next_steps"],
        "identity": {"task_version": VERSION, "completeness": "frozen",
                     "sample_versions": sample_versions, "method_versions": method_versions,
                     "reference_versions": reference_versions},
        "provenance": provenance,
    })
    require(len(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode()) + 1 <= MAX_BYTES,
            "Research projection exceeds supported size")
    return record


def preview(record):
    """Show a bounded summary; never dump hundreds of source rows to the terminal."""
    return {
        "status": "preview_only", "id": record["id"], "title": record["title"],
        "sample_count": record["sample_count"], "sample_unit": record["sample_unit"],
        "dataset": record["dataset"]["label"], "summary": record["summary"],
        "methods": [{"id": method["id"], "primary": method["metrics"][0],
                     "resources": [metric for metric in method["metrics"]
                                   if metric["label"] in ("推理总耗时", "进程显存峰值")]}
                    for method in record["methods"]],
        "findings_count": len(record["findings"]), "provenance_count": len(record["provenance"]),
        "fingerprint": record["fingerprint"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--conclusions", type=Path, help="Defaults to ROOT/conclusions.json")
    parser.add_argument("--preview", action="store_true", help="Validate and show a brief summary without registration")
    args = parser.parse_args()
    record = build(args.root, args.conclusions)
    print(json.dumps(preview(record) if args.preview else register_record(record), ensure_ascii=False, indent=2))
