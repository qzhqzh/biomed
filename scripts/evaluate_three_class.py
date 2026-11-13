#!/usr/bin/env python3
"""Evaluate frozen three-class outputs and register the reviewed public record."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.evidence_pilot import atomic_json
from apps.curation.research_history import register_record
from apps.curation.three_class_evaluation import evaluate, file_hash, read_json

LABELS = {"solid": "实体瘤", "hematologic": "血液肿瘤", "non_neoplastic": "非肿瘤"}


def fraction(label, numerator, denominator, note):
    return {
        "label": label,
        "numerator": numerator,
        "denominator": denominator,
        "note": note,
    }


def value(label, amount, note, unit="篇"):
    return {"label": label, "value": amount, "unit": unit, "note": note}


def public_record(evaluation, folder, explanation):
    methods = []
    for key, label, version in (
        (
            "rule",
            "规则 v3 · 保守三分类适配",
            "solid-tumor-rules-v3 / conservative-three-class-v1",
        ),
        (
            "model",
            "Qwen3-1.7B · 只输出一个分类",
            "literature-three-class-v1 / zero-shot",
        ),
        ("always_solid", "朴素基线 · 全部猜实体瘤", "always-solid-v1 / no-inference"),
    ):
        result = evaluation["methods"][key]
        metrics = [
            fraction(
                "三分类与参考一致",
                result["matched"],
                result["denominator"],
                "参考确定的29篇；错误、未完成和输出失败均保留在分母。",
            ),
            fraction(
                "分类完成覆盖",
                result["completed"],
                result["total"],
                "全部30篇中给出合法三类结果的比例；不是准确率。",
            ),
        ]
        metrics.extend(
            fraction(
                f"{LABELS[category]}参考识别",
                item["matched"],
                item["denominator"],
                "该参考类别内部的命中数，不是预测该类的精确率。",
            )
            for category, item in result["by_class"].items()
            if item["denominator"]
        )
        if result["macro_recall"] is not None:
            metrics.append(
                value(
                    "三类召回率均值",
                    round(result["macro_recall"] * 100, 1),
                    "三个类别的识别比例等权平均；减少多数实体瘤对总分的掩盖，仍受少量开发样本限制。",
                    "%",
                )
            )
        metrics.extend(
            [
                value(
                    "分类错误",
                    result["disagreed"],
                    "仅参考确定的样本中，已输出其他类别。",
                ),
                value(
                    "未完成判定",
                    result["statuses"].get("abstained", 0),
                    "全部30篇中没有可靠给出业务类别；不是第四类。",
                ),
                value(
                    "输出失败",
                    sum(
                        count
                        for status, count in result["statuses"].items()
                        if status
                        not in ("classified", "validated_candidate", "abstained")
                    ),
                    "全部30篇的协议失败，不能从评分分母剔除。",
                ),
            ]
        )
        if key == "model":
            metrics.extend(
                [
                    value(
                        "平均生成耗时",
                        evaluation["timing"]["mean_seconds"],
                        "30次尝试均计入；含GPU输入传输和生成，不含加载、分词、写盘、参考或导出。",
                        "秒/篇",
                    ),
                    value(
                        "模型装载",
                        evaluation["timing"]["load_seconds"],
                        "单独记录一次装载成本，不计入平均生成时间。",
                        "秒",
                    ),
                ]
            )
        methods.append(
            {
                "id": key,
                "label": label,
                "version": version,
                "metrics": metrics,
                "conclusion": {
                    "rule": "只复用已有关键词/正则规则；不能识别非肿瘤的旧scope保留未完成。",
                    "model": "总一致数低于全部猜实体瘤；血液和非肿瘤识别不足，不能接管自动筛选。",
                    "always_solid": "不读取文献、不调用模型，直接全部猜实体瘤；用于揭示类别不平衡，不能作为可用筛选器。",
                }[key],
            }
        )

    def provenance(path, label):
        path = Path(path)
        if not path.is_absolute():
            path = ROOT / path
        return {
            "label": label,
            "artifact": path.resolve().relative_to(ROOT).as_posix(),
            "sha256": file_hash(path),
        }

    model = evaluation["methods"]["model"]
    rules = evaluation["methods"]["rule"]
    record = {
        "schema_version": "research-history-v1",
        "id": "three-class-20261022",
        "task_kind": "three_class",
        "task_version": evaluation["task_version"],
        "title": "三分类 v1：固定30篇，只判断实体瘤、血液肿瘤、非肿瘤",
        "date": "2026-10-22",
        "stage": "classification",
        "status": "evaluated",
        "sample_count": 30,
        "summary": f"Qwen3-1.7B与Astra参考一致 {model['matched']}/{model['denominator']}；规则v3保守适配 {rules['matched']}/{rules['denominator']}；全部猜实体瘤也有24/29。少数类仍不足，不能接管自动筛选。",
        "task": {
            "question": "文献的实际研究对象属于实体瘤、血液肿瘤，还是非肿瘤？",
            "input": "标题、摘要、MeSH和出版类型；本次不读取全文。",
            "outputs": list(LABELS.values()),
            "not_measured": [
                "癌种和部位细分",
                "证据句与实体关系抽取",
                "后训练收益",
                "全库泛化准确率",
            ],
        },
        "dataset": {
            "label": "固定30篇开发测试集",
            "role": "development",
            "notes": [
                "沿用已冻结的同30篇文献，不新增采集；样本用于研究开发。",
                "Astra参考确定29篇：实体瘤24、血液肿瘤1、非肿瘤4；另1篇参考未决，不计正确或错误。",
                "类别分布不均衡，尤其血液肿瘤仅1篇；不能由总分判断真实全库能力。",
            ],
        },
        "reference": {
            "label": "Astra 三分类独立参考 v1",
            "status": "model_reference_not_human_gold",
            "note": "gpt-6-astra独立核读相同输入和冻结定义，未看本轮模型或规则输出。逐字引句与来源身份已核对；是可修订模型参考。",
        },
        "methods": methods,
        "findings": [
            f"小模型 {model['matched']}/{model['denominator']} 与参考一致，低于全部猜实体瘤的24/29；类别不平衡掩盖少数类问题，不能接管自动筛选，也不与旧九分类的0分直接比较。",
            "唯一血液肿瘤文献（慢性淋巴细胞白血病，PMID 42857718）被判为实体瘤；非肿瘤参考仅1/4识别正确。",
            '撤稿文献 PMID 42850778 输出 {"category":"null"}：字符串不等于JSON null，因此保留为1次格式失败，未偷偷修正或重跑。',
            "规则v3按关键词与正则命中归类；胆道癌表达 biliary tract cancer 漏过主路由式，未定性息肉可能受背景癌词误导。",
            "本轮没有训练。重复运行复用已完成输出，不再次加载GPU；文献分类之外的知识提取仍待测试。",
        ],
        "next_steps": [
            "先补足血液肿瘤和非肿瘤样本，保留未参与调参的评测材料。",
            "保持三类定义固定，再对照单一提示词或模型变化；明确失败原因后再决定是否后训练。",
        ],
        "identity": {
            "task_version": evaluation["task_version"],
            "sample_versions": [evaluation["input_sha256"]],
            "method_versions": [
                evaluation["config_sha256"],
                "conservative-three-class-v1",
                "always-solid-v1",
                *sorted({s["taxonomy_sha256"] for s in evaluation["snapshot_sources"]}),
            ],
            "reference_versions": [
                evaluation["reference_sha256"],
                evaluation["definition_sha256"],
            ],
            "completeness": "frozen",
        },
        "provenance": [
            provenance(folder / name, label)
            for name, label in (
                ("inputs.json", "统一30篇冻结输入"),
                ("task-definition-v1.json", "三分类任务定义和完整提示词"),
                ("astra-reference-v1.json", "Astra独立参考和原文引句"),
                ("run/manifest.json", "模型与运行配置"),
                ("evaluation.json", "逐篇对照与统一指标"),
            )
        ],
        "cases": evaluation["records"],
        "rules_explanation": explanation,
        "classification_definition": {
            "categories": LABELS,
            "boundary_notes": evaluation["definition"]["boundary_notes"],
        },
    }
    record["provenance"].extend(
        provenance(item["path"], "冻结规则输出及来源身份")
        for item in evaluation["snapshot_sources"]
    )
    if any(
        private in json.dumps(record, ensure_ascii=False)
        for private in ("/data/", "/appdata/", "/home/", "/tmp/")
    ):
        raise ValueError("Public research projection contains an absolute local path")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder", type=Path, default=ROOT / "var/experiments/three-class-20261022"
    )
    parser.add_argument(
        "--snapshots", type=Path, nargs="+",
        default=[
            ROOT / "var/experiments/evidence-pilot-20261022-first20",
            ROOT / "var/experiments/evidence-pilot-20261022-solid10",
        ],
    )
    parser.add_argument(
        "--rules-explanation", type=Path,
        default=ROOT / "var/experiments/three-class-20261022/rules-explanation-v3.json",
    )
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    evaluation = evaluate(args.folder, args.snapshots)
    # CLI spelling (absolute/relative paths or snapshot argument order) is not
    # an experiment change. Keep the serialized artifact and its provenance stable.
    for source in evaluation["snapshot_sources"]:
        source["path"] = Path(source["path"]).resolve().relative_to(ROOT).as_posix()
    evaluation["snapshot_sources"].sort(key=lambda source: source["path"])
    atomic_json(args.folder / "evaluation.json", evaluation)
    explanation = read_json(args.rules_explanation)
    if len(json.dumps(explanation, ensure_ascii=False).encode()) > 30 * 1024:
        raise ValueError("Rules explanation exceeds 30 KiB")
    explanation["three_class_adaptation"] = {
        "version": "conservative-three-class-v1",
        "mapping": {
            "solid_candidate": "solid",
            "solid_boundary_candidate": "solid",
            "hematologic_candidate": "hematologic",
        },
        "otherwise": "未完成：其余旧scope不强行解释为非肿瘤，也不新增第四类。",
    }
    cases = {row["pmid"]: row for row in evaluation["records"]}
    for example in explanation["examples"]:
        case = cases[example["pmid"]]
        example["reference_relevance"] = case["reference_relevance"]
        example["reference_status"] = "model_reference_not_human_gold"
        example["reference_label"] = "本轮Astra三分类参考v1"
        if example["pmid"] == "42871195":
            example["note"] = (
                "未定性息肉研究：背景中的carcinomas触发solid_malignancy，加adenomas/polyps得到solid_boundary；本轮三分类参考为非肿瘤，规则未识别背景角色。"
            )
    record = public_record(evaluation, args.folder, explanation)
    result = register_record(record) if args.register else {"status": "evaluated_only"}
    print(
        json.dumps(
            {
                "registration": result,
                "methods": evaluation["methods"],
                "timing": evaluation["timing"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
