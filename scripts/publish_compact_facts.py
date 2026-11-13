"""Project verified compact-fact research into the existing read-only history UI."""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.research_history import MAX_BYTES, prepare_record, register_record
from scripts.evaluate_compact_facts import build as build_evaluation
from scripts.evaluate_knowledge_candidates import freeze, read, sha

RECORD_ID = "compact-facts-five-papers-20261024"
PROTOCOL_NOTE = (
    "初版输出协议未通过，试跑已停止并另行留档；正式版本仍完整处理原15个片段组，"
    "没有挑选成功样本，初版失败试跑不并入正式15条分母。"
)


def strings(value, label):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must contain nonempty strings")
    return value


def note_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def ratio(label, numerator, denominator, note):
    if type(numerator) is not int or type(denominator) is not int:
        raise ValueError("Metric counts must be integers")
    if denominator == 0:
        if numerator:
            raise ValueError("Positive numerator with empty denominator")
        return {"label": label, "value": "无可计数输出", "note": note}
    if not 0 <= numerator <= denominator:
        raise ValueError("Invalid metric denominator")
    return {
        "label": label,
        "numerator": numerator,
        "denominator": denominator,
        "note": note,
    }


def seconds(label, value, note):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid measured time")
    return {"label": label, "value": round(value, 2), "unit": "秒", "note": note}


def stage_metrics(metrics, duration, core, *, stage):
    count = metrics["packets"]
    entity_count = metrics.get(
        "raw_compact_entity_mentions", metrics["entity_mentions"]
    )
    result = [
        ratio(
            "完整通过（主指标）",
            metrics["joint_pass"],
            count,
            "原v1结构/出处检查通过，且Astra整条supported；弃答、非法或信息不完整均保留在15条分母。",
        ),
        ratio(
            "完整结构与出处检查",
            metrics["schema_and_provenance_pass"],
            count,
            "所有最终字段仍接受006原契约检查；定位成功不代表医学语义正确。",
        ),
        ratio(
            "Astra整条语义支持",
            metrics["semantic_verdicts"].get("supported", 0),
            count,
            "核对中心发现、必要实体、完整方案、组别、条件、数值和字段证据；模型参考，非人工金标准。",
        ),
        ratio(
            "中心陈述获支持（诊断）",
            core,
            count,
            "只看中心陈述的诊断指标，不能替代整条通过率或发布门槛。",
        ),
        ratio(
            "实体严格定位",
            metrics["entity_exact_span_count"],
            entity_count,
            "全部原始实体数组项为分母，含非法项和组装失败输出；仅计算通过最终校验的定位，不代表类型/关系正确。",
        ),
        ratio(
            "唯一精确别名链接",
            metrics["unique_exact_alias_links"],
            entity_count,
            "仅为现有MONDO/RxNorm/NCBIGene字典的精确别名候选，不是标准化准确率或目标内部UUID。",
        ),
        ratio(
            "可组装候选的精确引句",
            metrics["evidence_exact_span_count"],
            metrics["evidence_quotes"],
            "仅对已形成canonical字段的引句统计；失败输出没有合法引句，不据此抬高整条通过率。精确引用不等于语义支持。",
        ),
        ratio(
            "可直接导入tumor_api",
            metrics["mapping_api_ready"],
            count,
            "目标UUID、非空触发器和完整溯源适配未完成；全部仍为不可提交的研究预览。",
        ),
        seconds(
            "总处理时间",
            duration,
            "006为原逐条推理总和；新方案包含截至本阶段全部推理及逐条脚本组装/链接。加载、预热和评测独立，不代表线上吞吐。",
        ),
        {
            "label": "平均每组",
            "value": round(duration / count, 2),
            "unit": "秒/组",
            "note": "固定15个片段组，不剔除失败或弃答。",
        },
    ]
    if stage != "baseline":
        result.append(
            ratio(
                "本阶段合法JSON",
                metrics["rawjson_statuses"].get("classified", 0),
                count,
                "抽取后看抽取JSON，核验后看核验JSON；仅格式统计，不等于核验通过或知识正确。",
            )
        )
    return result


def statement(row):
    candidate = row.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("statement"), str):
        return candidate["statement"]
    return None


def display_statement(row):
    canonical = statement(row)
    if canonical:
        return canonical
    for key, label in (
        ("raw_compact", "未通过组装的原始草稿（仅诊断）："),
        ("rejected_draft_compact", "原抽取草稿（定向核验失败，未形成最终候选）："),
    ):
        compact = row.get(key)
        fact = compact.get("fact") if isinstance(compact, dict) else None
        claim = fact.get("claim") if isinstance(fact, dict) else None
        if isinstance(claim, list) and claim:
            claim = claim[0]
        if isinstance(claim, str) and claim.strip():
            return label + claim
    return None


def review_label(row):
    return "完整通过" if row["joint_pass"] else "未完整通过"


def public_case(before, after):
    candidate = after["candidate"] if isinstance(after["candidate"], dict) else {}
    quotes = []
    for span in after["validation"]["evidence_spans"]:
        matches = [
            {"start": match["start"], "end": match["end"]} for match in span["matches"]
        ]
        quotes.append(
            {
                **{
                    key: span[key]
                    for key in (
                        "evidence_index",
                        "passage_id",
                        "locator",
                        "quote",
                        "fields",
                        "ambiguous",
                        "source_sha256",
                        "version_id",
                        "parser_version",
                        "coordinate_space",
                    )
                },
                "matches": matches,
                "start": matches[0]["start"] if len(matches) == 1 else None,
                "end": matches[0]["end"] if len(matches) == 1 else None,
            }
        )
    transition = (
        "新增通过"
        if after["joint_pass"] and not before["joint_pass"]
        else (
            "核验后失去通过"
            if before["joint_pass"] and not after["joint_pass"]
            else "通过状态未变"
        )
    )
    review_note = (
        f"抽取后：{review_label(before)}；定向核验后：{review_label(after)}（{transition}）。"
        f" 抽取后Astra：{before['review']['note']} 核验后Astra：{after['review']['note']}"
    )
    if statement(before) and statement(before) != statement(after):
        review_note += f" 抽取后原陈述：{statement(before)}"
    structural_errors = sorted(
        {error["code"] for error in after["validation"]["errors"]}
    )
    assembly_errors = sorted({error["code"] for error in after["assembly_errors"]})
    if structural_errors or assembly_errors:
        review_note += (
            " 最终结构/定位问题："
            + "、".join(sorted(set(structural_errors + assembly_errors)))
            + "。"
        )
    review_note += f" 累计处理{after['seconds']:.2f}秒。"
    status = after["review"]["verdict"]
    if after["validation"]["status"] == "abstained":
        status = "abstained"
    elif after["validation"]["status"] != "structurally_valid_candidate":
        status = "invalid_output"
    mapping = after["mapping"]
    if mapping.get("api_ready") is not False or mapping.get("status") != "blocked":
        raise ValueError("Research mapping must remain explicitly non-importable")
    # Whitelist the only target fields produced by the existing mapping adapter.
    allowed_preview = {
        "evidence_description",
        "evidence_description_en",
        "evidence_category",
        "evidence_source",
        "evidence_level",
        "evidence_type",
        "comment",
        "conclusion_inputs",
    }
    preview = {
        key: value
        for key, value in mapping["preview"].items()
        if key in allowed_preview
    }
    if "conclusion_inputs" in preview:
        preview["conclusion_inputs"] = [
            {
                key: conclusion[key]
                for key in (
                    "potency",
                    "drug_ids",
                    "disease_scope_inputs",
                    "trigger_inputs",
                )
            }
            for conclusion in preview["conclusion_inputs"]
        ]
    return {
        "id": after["id"],
        "pmid": after["article"]["pmid"],
        "title": after["article"]["title"],
        "statement": display_statement(after),
        "status": status,
        "review_note": review_note,
        "study_scope": candidate.get("study_scope", "unclear"),
        "source_role": candidate.get("source_role", "unclear"),
        "entities": [
            {
                key: entity[key]
                for key in (
                    "id",
                    "type",
                    "text",
                    "passage_id",
                    "source_anchored",
                    "normalization_status",
                    "standard_ids",
                )
            }
            for entity in after["normalized_entities"]
        ],
        "quotes": quotes,
        "mapping": {
            "status": "blocked",
            "api_ready": False,
            "blockers": strings(mapping["blockers"], "mapping blockers"),
            "preview": preview,
        },
        "article_provenance": {
            key: after["article"].get(key)
            for key in (
                "pmid",
                "pmcid",
                "doi",
                "title",
                "material_id",
                "version_id",
                "source_sha256",
                "source_url",
                "rights",
                "acquired_at",
            )
        },
        "input_sha256": after["input_sha256"],
    }


def build(base):
    base = Path(base)
    evaluation = read(base / "evaluation.json")
    sources = evaluation.get("sources", {})
    if not all(key in sources for key in ("base", "baseline", "standards", "reviews")):
        raise ValueError("Evaluation lacks reproducible source identity")
    if Path(sources["base"]).resolve() != base.resolve():
        raise ValueError("Evaluation belongs to a different experiment")
    verified = build_evaluation(
        base, sources["baseline"], sources["standards"], sources["reviews"]
    )
    if digest(evaluation) != digest(verified):
        raise ValueError(
            "Evaluation differs from verified frozen artifacts; refusing stale metrics"
        )
    baseline = Path(sources["baseline"])
    old = read(baseline / "evaluation.json")
    notes = read(base / "research-notes.json")
    if notes.get("evaluation_sha256", sha(base / "evaluation.json")) != sha(
        base / "evaluation.json"
    ):
        raise ValueError("Notes refer to a different evaluation")
    summary = note_text(notes.get("summary"), "summary")
    findings = strings(notes.get("findings"), "findings")
    next_steps = strings(notes.get("next_steps"), "next_steps")
    before, after = (evaluation["stages"][stage] for stage in ("before", "after"))
    if len(before["records"]) != 15 or len(after["records"]) != 15:
        raise ValueError("Public record must retain all 15 source packets")
    after_rows = {row["id"]: row for row in after["records"]}
    if len(after_rows) != 15 or set(after_rows) != {
        row["id"] for row in before["records"]
    }:
        raise ValueError("Before/after packet coverage differs")
    cases = [public_case(row, after_rows[row["id"]]) for row in before["records"]]
    conclusions = notes.get("method_conclusions", {})
    if not isinstance(conclusions, dict):
        raise TypeError("method_conclusions must be an object")
    fallback = notes.get(
        "method_conclusion", "同一开发集上的流程比较；以整条通过率和累计耗时共同判断。"
    )
    methods = []
    old_core = sum(
        row["review"].get("core_statement_supported") is True for row in old["records"]
    )
    entries = [
        (
            "baseline",
            "006 · 9B一次输出完整记录",
            old["metrics"],
            old["timing"]["total_inference_seconds"],
            old_core,
            old["timing"]["config_sha256"],
        ),
        (
            "before",
            "007 · 9B紧凑抽取 + 脚本组装",
            before["metrics"],
            evaluation["timing"]["pipeline_seconds"]["before"],
            before["metrics"]["core_statement_supported"],
            evaluation["timing"]["extract"]["config_sha256"],
        ),
        (
            "after",
            "007 · 再作9B定向核验 + 脚本重组",
            after["metrics"],
            evaluation["timing"]["pipeline_seconds"]["after"],
            after["metrics"]["core_statement_supported"],
            digest(
                {
                    "extract": evaluation["identity"]["extract"],
                    "verify": evaluation["identity"]["verify"],
                    "assembler": evaluation["identity"]["assembler_sha256"],
                }
            ),
        ),
    ]
    for stage, label, metrics, duration, core, version in entries:
        method = {
            "id": f"compact-{stage}",
            "label": label,
            "version": version,
            "metrics": stage_metrics(metrics, duration, core, stage=stage),
            "conclusion": note_text(
                conclusions.get(stage, fallback), f"{stage} conclusion"
            ),
        }
        if stage == "after":
            method["metrics"].append(
                seconds(
                    "定向核验额外耗时",
                    evaluation["timing"]["pipeline_seconds"]["after"]
                    - evaluation["timing"]["pipeline_seconds"]["before"],
                    "全部15次定向核验推理，加核验后的脚本组装/链接；没有只计算成功修订。",
                )
            )
        methods.append(method)
    gained = evaluation["targeted_review"]["joint_gained"]
    lost = evaluation["targeted_review"]["joint_lost"]
    comparison_note = (
        f"定向核验前后：新增完整通过{len(gained)}条（{'、'.join(gained) or '无'}），"
        f"失去完整通过{len(lost)}条（{'、'.join(lost) or '无'}）；同15组全部保留。"
    )
    provenance = [
        {
            "label": label,
            "artifact": str((base / name).resolve()),
            "sha256": sha(base / name),
        }
        for label, name in (
            ("完整前后候选、检查与计时", "evaluation.json"),
            ("原始15组输入", "source-inputs.json"),
            ("确定性原文单元", "units.json"),
            ("紧凑抽取协议", "extract-definition.json"),
            ("定向核验协议", "verify-definition.json"),
            ("评测结论", "research-notes.json"),
        )
    ]
    provenance.extend(
        {
            "label": label,
            "artifact": str((baseline / name).resolve()),
            "sha256": sha(baseline / name),
        }
        for label, name in (
            ("006原评价结果", "evaluation.json"),
            ("只读核对的目标结构", "target-contract-verified.json"),
        )
    )
    failure_artifact = notes.get("protocol_failure_artifact")
    protocol_detail = None
    if failure_artifact:
        path = Path(failure_artifact)
        path = path if path.is_absolute() else base / path
        path = path.resolve()
        if (
            not path.is_relative_to((ROOT / "var/experiments").resolve())
            or not path.is_file()
        ):
            raise ValueError(
                "Protocol failure artifact must be an existing research artifact"
            )
        failure = read(path)
        if (
            failure.get("status") != "stopped_protocol_diagnostic"
            or failure.get("included_in_formal_comparison") is not False
        ):
            raise ValueError(
                "Expected a stopped protocol diagnosis outside the formal comparison"
            )
        failed_seconds = seconds(
            "初版试跑推理", failure["total_inference_seconds"], "独立研究成本"
        )
        protocol_detail = (
            f"初版在{failure['completed']}组后停止，合法JSON {failure['model_statuses'].get('classified', 0)}组，"
            f"完整结构通过{failure['structural_pass']}组，额外推理{failed_seconds['value']:.2f}秒。"
            "此失败成本单列，不合并到正式版本的每组耗时。"
        )
        provenance.append(
            {
                "label": "初版协议失败留档（不并入正式成绩）",
                "artifact": str(path),
                "sha256": sha(path),
            }
        )
    record = {
        "schema_version": "research-history-v1",
        "id": RECORD_ID,
        "date": "2026-10-24",
        "stage": "entities",
        "status": "evaluated",
        "task_kind": "knowledge_candidates",
        "sample_count": 15,
        "sample_unit": "组",
        "title": "007 · 紧凑事实、脚本组装与定向核验",
        "summary": summary,
        "task": {
            "question": "同15组文本下，紧凑抽取、脚本组装和定向核验能否改善完整通过率与总耗时？",
            "input": "与006字节相同的5篇论文、15个片段组；原ANCHOR/SUPPORT及上游入选结果不变。",
            "outputs": [
                "紧凑事实与实体关系",
                "脚本定位及原v1字段组装",
                "组别、条件和数值的一次定向核验",
                "前后完整通过率和全部处理耗时",
                "文章与片段两层出处及不可提交映射",
            ],
            "not_measured": [
                "独立新论文泛化",
                "全篇/全库抽取召回率",
                "微调收益",
                "生产API写入或临床适用性",
            ],
        },
        "dataset": {
            "label": "5篇实体瘤治疗论文的同15个开发片段组",
            "role": "已用于改进的开发回归集",
            "document_count": 5,
            "notes": [
                PROTOCOL_NOTE,
                "每组最多一个中心发现；弃答及失败不剔除，不扩充或挑选样本。",
                "句段单元从原文本确定性切分，保留原段落及字符坐标；未引入上游漏检段落。",
            ],
        },
        "reference": {
            "label": "复用006冻结Astra参考，独立核验前后两版",
            "status": "model_reference_not_human_gold",
            "note": "旧参考和旧定义哈希原样保留；9B定向核验不读取Astra答案。Astra对前后整条记录评审，非人工金标准或盲法评审。",
        },
        "methods": methods,
        "findings": [
            comparison_note,
            PROTOCOL_NOTE,
            *([protocol_detail] if protocol_detail else []),
            *findings,
        ],
        "next_steps": next_steps,
        "identity": {
            "task_version": evaluation["version"],
            "sample_versions": [
                evaluation["identity"]["source_inputs_sha256"],
                evaluation["identity"]["units_sha256"],
            ],
            "method_versions": [entry[5] for entry in entries]
            + [
                evaluation["identity"]["assembler_sha256"],
                evaluation["identity"]["validator_sha256"],
            ],
            "reference_versions": evaluation["identity"]["baseline_reference_sha256"]
            + evaluation["identity"]["review_sha256"],
            "completeness": "frozen",
        },
        "provenance": provenance,
        "knowledge_candidates": cases,
    }
    prepare_record(record)
    if (
        len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode())
        > MAX_BYTES
    ):
        raise ValueError("Research projection exceeds the existing UI record limit")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Immutable local projection; defaults to BASE/research-record.json",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Register the verified projection in the existing research history",
    )
    args = parser.parse_args()
    record = build(args.base)
    destination = args.output or args.base / "research-record.json"
    freeze(destination, record)
    result = (
        register_record(record)
        if args.publish
        else {"status": "prepared", "id": record["id"], "output": str(destination)}
    )
    print(json.dumps(result, ensure_ascii=False))
