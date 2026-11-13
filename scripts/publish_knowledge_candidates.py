"""Publish a compact, read-only research projection, not clinical knowledge."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.curation.research_history import register_record
from scripts.evaluate_knowledge_candidates import build as build_evaluation
from scripts.evaluate_knowledge_candidates import freeze, read, sha

RECORD_ID = "knowledge-candidates-five-papers-20261023"


def build(base):
    evaluation = read(base / "evaluation.json")
    sources = evaluation.get("sources", {})
    if not all(key in sources for key in ("model_output", "standards", "reviews")):
        raise ValueError("Evaluation lacks reproducible source identities")
    verified = build_evaluation(
        base, sources["model_output"], sources["standards"], sources["reviews"]
    )
    if evaluation != verified:
        raise ValueError(
            "Evaluation differs from verified frozen artifacts; do not publish stale or edited metrics"
        )
    metrics = evaluation["metrics"]
    timing = evaluation["timing"]
    inputs = {row["id"]: row for row in read(base / "inputs.json")["records"]}
    notes = read(base / "research-notes.json")
    if notes.get("evaluation_sha256", sha(base / "evaluation.json")) != sha(
        base / "evaluation.json"
    ):
        raise ValueError("Research notes refer to a different evaluation")
    cases = []
    for row in evaluation["records"]:
        candidate = row["candidate"] if isinstance(row["candidate"], dict) else {}
        packet = inputs[row["id"]]
        candidate = {
            key: value for key, value in candidate.items() if isinstance(value, str)
        }
        quotes = []
        for span in row["validation"]["evidence_spans"]:
            matches = span["matches"]
            quotes.append(
                {
                    "evidence_index": span["evidence_index"],
                    "passage_id": span["passage_id"],
                    "locator": span["locator"],
                    "quote": span["quote"],
                    "fields": span["fields"],
                    "matches": matches,
                    "ambiguous": span["ambiguous"],
                    "source_sha256": span["source_sha256"],
                    "version_id": span["version_id"],
                    "parser_version": span["parser_version"],
                    "coordinate_space": span["coordinate_space"],
                    "start": matches[0]["start"] if len(matches) == 1 else None,
                    "end": matches[0]["end"] if len(matches) == 1 else None,
                }
            )
        cases.append(
            {
                "id": row["id"],
                "pmid": row["article"]["pmid"],
                "title": row["article"]["title"],
                "statement": candidate.get("statement"),
                "status": row["review"]["verdict"],
                "review_note": row["review"]["note"]
                + "；结构/引用检查："
                + (
                    "弃权"
                    if row["validation"]["status"] == "abstained"
                    else "通过"
                    if row["validation"]["valid"]
                    else "未通过："
                    + ", ".join(e["code"] for e in row["validation"]["errors"])
                ),
                "study_scope": candidate.get("study_scope", "unclear"),
                "source_role": candidate.get("source_role", "unclear"),
                "entities": [
                    {
                        k: e[k]
                        for k in (
                            "id",
                            "type",
                            "text",
                            "passage_id",
                            "source_anchored",
                            "normalization_status",
                            "standard_ids",
                        )
                    }
                    for e in row["normalized_entities"]
                ],
                "quotes": quotes,
                "entity_spans": row["validation"]["entity_spans"],
                "field_supports": row["validation"]["field_supports"],
                "mapping": row["mapping"],
                "article_provenance": row["article"],
                "input_sha256": packet["input_sha256"],
            }
        )
    count = metrics["packets"]
    supported = metrics["semantic_verdicts"].get("supported", 0)
    core_reviews = [
        row["review"]
        for row in evaluation["records"]
        if type(row["review"].get("core_statement_supported")) is bool
    ]
    ratio = lambda label, n, note: {
        "label": label,
        "numerator": n,
        "denominator": count,
        "note": note,
    }
    items = [
        ratio(
            "合法JSON输出",
            metrics["model_statuses"].get("classified", 0),
            "全部固定片段组为分母，包含合法弃权；JSON合法不等于知识正确。",
        ),
        ratio(
            "完整结构与引用检查",
            metrics["schema_and_provenance_pass"],
            "包含字段、实体引用、精确引句、来源版本和字段支持标签；不是语义正确率。",
        ),
        ratio(
            "Astra整条核验通过",
            supported,
            "核验所选中心结论、条件、实体关系和引用支持；Astra为模型参考，非人工金标准。",
        ),
        ratio(
            "结构与语义均通过",
            metrics["joint_pass"],
            "本轮候选质量门槛；仍未正式发布。",
        ),
        {
            "label": "实体原文定位",
            **(
                {
                    "numerator": metrics["entity_exact_span_count"],
                    "denominator": metrics["entity_mentions"],
                }
                if metrics["entity_mentions"]
                else {"value": 0, "unit": "条"}
            ),
            "note": "原始实体提及能在指定段落精确定位的比例；不代表实体类型、关系或标准化正确。",
        },
        {
            "label": "精确引用与字段标签",
            **(
                {
                    "numerator": metrics["evidence_exact_span_count"],
                    "denominator": metrics["evidence_quotes"],
                }
                if metrics["evidence_quotes"]
                else {"value": 0, "unit": "条"}
            ),
            "note": "原始引句中，可精确定位且字段标签合法的比例；不代表引句在语义上支持这些字段，也不代表全部字段均已引用。",
        },
        {
            "label": "实体精确别名链接",
            **(
                {
                    "numerator": metrics["unique_exact_alias_links"],
                    "denominator": metrics["entity_mentions"],
                }
                if metrics["entity_mentions"]
                else {"value": 0, "unit": "条"}
            ),
            "note": "全部原始实体提及为分母，未定位提及保留失败；只统计现有字典唯一精确别名，不是实体链接准确率。",
        },
        ratio(
            "可直接导入目标",
            metrics["mapping_api_ready"],
            "缺内部UUID、分子触发条件或目标无法表达的信息时明确阻塞。",
        ),
        {
            "label": "总推理时间",
            "value": round(timing["total_inference_seconds"], 2),
            "unit": "秒",
            "note": "含分词、传输、生成、JSON校验；不含加载、预热和落盘。",
        },
        {
            "label": "平均每条",
            "value": round(timing["mean_seconds"], 2),
            "unit": "秒/条",
            "note": (
                "包含重启后断点续跑的batch=1逐条计时，不能当作单次连续吞吐。"
                if timing.get("timing_resumed")
                else "一次连续batch=1实测，不是部署吞吐承诺。"
            ),
        },
        {
            "label": "模型加载（单列）",
            "value": round(
                sum(s.get("load_seconds", 0) for s in timing.get("sessions", []))
                or timing.get("load_seconds", 0),
                2,
            ),
            "unit": "秒",
            "note": "各已记录会话加载耗时合计；受当时磁盘I/O影响，不含未完成加载，未隔离操作系统缓存。",
        },
        {
            "label": "峰值显存",
            "value": round(timing.get("nvml_process_peak_mib", 0) / 1024, 2),
            "unit": "GiB",
            "note": "NVML进程峰值，0.1秒采样。",
        },
    ]
    if len(core_reviews) == count:
        items.insert(
            2,
            ratio(
                "中心陈述获支持（诊断）",
                sum(row["core_statement_supported"] for row in core_reviews),
                "事后拆分诊断：仅看中心陈述，仍要求中心段原文支持、组别及范围正确；不代表实体、条件、引用等整份输出合格。",
            ),
        )
    return {
        "schema_version": "research-history-v1",
        "id": RECORD_ID,
        "date": "2026-10-23",
        "stage": "entities",
        "status": "evaluated",
        "task_kind": "knowledge_candidates",
        "sample_count": count,
        "sample_unit": "条",
        "title": "006 · 5篇实体瘤文献的实体、关系与条件联合抽取",
        "summary": notes["summary"],
        "task": {
            "question": "9B能否从上一环节入选文本一次形成带两层出处、可映射的知识候选？",
            "input": "5篇已有全文，15个目的性片段组，26个不同段落；全部来自真实S2 9B阳性。",
            "outputs": [
                "实体原文跨度",
                "关系、条件与结果",
                "文章+段落及字段级出处",
                "标准实体候选",
                "tumor_api只读结构映射预览",
            ],
            "not_measured": [
                "全篇/全库抽取召回率",
                "拆分模型调用相对收益",
                "新论文泛化",
                "生产API写入或临床适用性",
            ],
        },
        "dataset": {
            "label": f"{metrics['documents']}篇实体瘤治疗论文的{count}个开发片段组",
            "role": "目的性开发集",
            "document_count": metrics["documents"],
            "notes": [
                "不强制每篇3条；单组最多一条核心结论，可弃答。",
                "原始段落、S2决策和文章版本保留；未从上游阴性段落偷补输入。",
                "输入上下文由本次策划选择；尚未实现全自动片段组装。",
            ],
        },
        "reference": {
            "label": "gpt-6-astra独立参考及逐候选复核",
            "status": "model_reference_not_human_gold",
            "note": "参考先冻结，再看9B输出核验；允许不同但有原文支持的中心结论。复核阶段不声称盲法。",
        },
        "methods": [
            {
                "id": "qwen35-9b-joint",
                "label": "Qwen3.5-9B联合抽取 + 确定性校验",
                "version": timing["config_sha256"],
                "metrics": items,
                "conclusion": notes["method_conclusion"],
            }
        ],
        "findings": notes["findings"],
        "next_steps": notes["next_steps"],
        "identity": {
            "task_version": evaluation["version"],
            "sample_versions": [evaluation["identity"]["inputs_sha256"]],
            "method_versions": [
                timing["config_sha256"],
                evaluation["identity"]["target_contract_sha256"],
            ],
            "reference_versions": evaluation["identity"]["reference_sha256"]
            + evaluation["identity"]["review_sha256"],
            "completeness": "frozen",
        },
        "provenance": [
            {"label": label, "artifact": str(base / name), "sha256": sha(base / name)}
            for label, name in [
                ("完整候选、检查与映射", "evaluation.json"),
                ("冻结输入", "inputs.json"),
                ("抽取定义", "definition.json"),
                ("目标源码合同", "target-contract-verified.json"),
                ("评测结论", "research-notes.json"),
            ]
        ],
        "knowledge_candidates": cases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    record = build(args.base)
    freeze(args.base / "research-record.json", record)
    if args.publish:
        print(json.dumps(register_record(record), ensure_ascii=False))
