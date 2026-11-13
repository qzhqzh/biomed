"""Publish the measured, non-production comparison in research history."""

import hashlib
import json
from pathlib import Path

from apps.curation.research_history import register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/knowledge-target-optimization-20261112-v1"
DOC = PROJECT / "docs/research/032-effective-knowledge-diagnosis.md"
RID = "effective-knowledge-diagnosis-20261112"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish():
    development_path = ROOT / "source-linked-target-development-comparison-v2.json"
    holdout_path = ROOT / "source-linked-target-holdout-comparison-v1.json"
    notes_path = ROOT / "holdout-source-only-abstract-observations.json"
    diagnosis_path = ROOT / "extraction-diagnostics-v1.json"
    development, holdout = read(development_path), read(holdout_path)
    manifest, observations, diagnostics = read(ROOT / "manifest.json"), read(notes_path), read(diagnosis_path)
    measured = (development["sample_count"], development["baseline_offline_structure_count"],
                development["new_offline_structure_count_including_preserved_baseline"], holdout["sample_count"],
                holdout["baseline_offline_structure_count"], holdout["new_offline_structure_count_including_preserved_baseline"])
    if measured != (6, 1, 2, 10, 0, 0):
        raise ValueError("Frozen comparison no longer matches the published description")
    if (holdout["normalization_statuses"].get("skipped_no_evidence") != 2
            or len(observations["records"]) != 10 or observations["model_outputs_consulted"]
            or {r["outcome"] for r in diagnostics["records"]} != {"entity_output_truncated", "model_returned_no_fact"}):
        raise ValueError("Required diagnostic evidence is missing")
    record = {
        "schema_version": "research-history-v1", "id": RID,
        "title": "032 · 可用知识诊断：结构改善与留出对照", "date": holdout["model_summary"]["updated_at"],
        "stage": "knowledge", "status": "evaluated", "task_kind": "target_compatibility",
        "sample_count": 16, "sample_unit": "条候选",
        "summary": "开发诊断样本离线结构通过1→2；留出10条0→0。尚无Astra独立核验，未证明新增有效知识，原型未接入后台。",
        "task": {
            "question": "按文章和段落绑定实体、分子事件与治疗关系，能否增加目标可表达的独立知识？",
            "input": "冻结30条旧候选，本次测试6条开发诊断样本和10条不同文献的留出候选；沿用现有9B和Bonsai。",
            "outputs": ["旧结果与带出处新结构的同样本对照", "不覆盖旧数据的目标结构预览", "区分9B输出截断和模型正常未找到事实"],
            "not_measured": ["Astra独立医学语义核验、临床准确率及真实患者匹配", "留出全文的完整正例数及全库召回", "CNV、融合、联合治疗和复杂跨段关系的完整覆盖"]},
        "dataset": {"label": "30条冻结候选中的16条对照", "role": "development_and_holdout",
                    "document_count": 16, "notes": ["开发6条为诊断选择，不代表总体分布。", "留出10条中2条无证据段落，未消耗模型推理。", "一条旧批次缺少基因标准版本，已单独标识。"]},
        "reference": {"label": "原文摘要预读与出处机械校验", "status": "not_independent_gold",
                      "note": "当前助手在查看模型结果前记录摘要观察；尚非完整全文、Astra或临床专家核验。模型字段与原文的语义正确率未测。"},
        "methods": [
            {"id": "old", "label": "原提取与原目标适配", "version": "frozen-original", "metrics": [
                {"label": "开发样本离线结构通过", "numerator": 1, "denominator": 6, "note": "原对照不是医学质量评价。"},
                {"label": "留出样本离线结构通过", "numerator": 0, "denominator": 10, "note": "包含2条没有可引用证据段落的旧记录。"}],
             "conclusion": "保留全部旧输出及原有通过结果。"},
            {"id": "new", "label": "来源绑定与窄范围目标适配原型", "version": "biomed.source-linked-target-preview.v1", "metrics": [
                {"label": "开发样本离线结构通过", "numerator": 2, "denominator": 6, "note": "含1条旧对照与1条新增结构候选；新增未独立核验。"},
                {"label": "留出样本离线结构通过", "numerator": 0, "denominator": 10, "note": "8条推理、2条无证据未推理；不能据此推断全文不存在正例。"},
                {"label": "留出关系推理耗时", "value": holdout["model_summary"]["total_inference_seconds"], "unit": "秒", "note": "8次热推理，不含加载、预热和完整流水线。"},
                {"label": "留出显存峰值", "value": holdout["model_summary"]["nvml_process_peak_mib"], "unit": "MiB", "note": "单次模型进程实测。"}],
             "conclusion": "开发案例可形成结构；留出没有净增，真实可用知识提升尚未证明，不接入默认后台。"}],
        "findings": [
            "复发性低级别浆液性卵巢癌病例：KRAS G12C、sotorasib 与部分缓解可追溯到同文不同摘要段；旧中心段限制和主癌种映射导致漏过。",
            "模型输出的出处绑定不能自动证明语义；病例亚型、既往治疗等条件未由目标触发字段完全执行。",
            "同篇多个相似片段可能重复一条关系，不能按候选行数计作独立知识。",
            "一条9B实体输出因长度截断；另一条关系模型正常返回空事实。分别诊断，不把正常空结果误报为系统失败。",
            "留出8次模型推理中6次字段/出处不完整、1次JSON无效、1次为来源绑定成功但不属于当前适配范围。"],
        "next_steps": ["只补充明确有分子事件和临床关系的原发性正例及负例，用Astra基于原文独立核验。", "核验净新增、去重和必要条件，达到接入标准后再考虑批次边界切换；现有后台继续原方法。"],
        "identity": {"task_version": "effective-knowledge-diagnosis-v1",
                     "sample_versions": [manifest["development_sha256"], manifest["holdout_sha256"]],
                     "method_versions": [development["inference_config_sha256"], holdout["inference_config_sha256"],
                                         holdout["code_sha256"]["apps/curation/source_linked_target_mapping.py"]],
                     "reference_versions": [holdout["dictionary_sha256"], sha(notes_path)], "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("开发样本对照", development_path), ("留出样本对照", holdout_path), ("摘要预读", notes_path),
            ("失败原因诊断", diagnosis_path), ("研究说明", DOC))],
    }
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
