"""Register the bounded study-context counterexample in research history."""

import hashlib
import json
from pathlib import Path

from apps.curation.research_history import register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/knowledge-target-optimization-20261112-v1/context-probe-v1"
RID = "study-context-guard-20261112"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish():
    comparison_path, context_path = ROOT / "comparison.json", ROOT / "study-context-v1.json"
    source_path = ROOT / "source-review.json"
    comparison, context, source = read(comparison_path), read(context_path), read(source_path)
    if ((comparison["sample_count"], comparison["baseline_offline_structure_count"],
         comparison["new_offline_structure_count_including_preserved_baseline"]) != (3, 0, 0)
            or (context["review_primary_conflicts"], context["preclinical_source_populations"],
                context["treatment_receipt_results"]) != (1, 1, 1)
            or source["model_probe_outputs_consulted"]):
        raise ValueError("The frozen counterexamples differ from the research description")
    doc = PROJECT / "docs/research/033-study-context-guard.md"
    record = {
        "schema_version": "research-history-v1", "id": RID,
        "title": "033 · 研究来源、动物模型与用药比例", "date": comparison["model_summary"]["updated_at"],
        "stage": "knowledge", "status": "evaluated", "task_kind": "target_compatibility",
        "sample_count": 3, "sample_unit": "条反例",
        "summary": "三条针对性反例均未放行；补充识别综述误标原发、PDX模型和用药比例，不证明有效知识增加。",
        "task": {"question": "如何避免把综述、动物模型或用药人数误当成人体疗效？",
                 "input": "事后选取的三条真实旧候选及其原文、Bonsai输出和文献元数据。",
                 "outputs": ["研究类型冲突提示", "研究对象及结果类型标记", "原始来源、模型结果和离线结构对照"],
                 "not_measured": ["Astra或临床专家核验", "随机样本准确率", "有效知识净新增"]},
        "dataset": {"label": "三条事后选择的困难反例", "role": "post_hoc_probe", "document_count": 3,
                    "notes": ["案例选取依赖既有候选，不是随机样本。", "三条原本均被其他检查拦住，不能宣称放行错误率下降。"]},
        "reference": {"label": "源文段落核读", "status": "model_reference_not_human_gold",
                      "note": "当前助手在此次模型输出前核读原文，不是Astra或临床专家。"},
        "methods": [
            {"id": "old", "label": "原离线原型", "version": "biomed.source-linked-target-preview.v1",
             "metrics": [{"label": "离线结构通过", "numerator": 0, "denominator": 3,
                          "note": "三条被其他检查拦住，综述来源误标当时未单独说明。"}],
             "conclusion": "没有新增结构，模型仍可能把综述说成原发。"},
            {"id": "context", "label": "增加研究来源和对象提示", "version": "biomed.source-study-context.v1",
             "metrics": [{"label": "发现综述与原发冲突", "numerator": 1, "denominator": 1, "note": "仅此一条事后选择的综述；不代表全库准确率。"},
                         {"label": "发现PDX研究对象", "numerator": 1, "denominator": 1, "note": "仅此一条事后选择的临床前反例。"},
                         {"label": "识别用药人数比例", "numerator": 1, "denominator": 1, "note": "只识别既有事实字段，不自动证明原文语义。"},
                         {"label": "离线结构通过", "numerator": 0, "denominator": 3, "note": "仍无新增目标结构。"}],
             "conclusion": "来源诊断改善；目前不能推断知识准确率、净产出或默认部署收益。"}],
        "findings": ["综述转述试验被模型误标为原发，但先前恰因其他检查未通过。", "PDX是临床前对象，不应改写为患者疗效。", "11/14是患者接受治疗的比例，不是疗效；同段存在其他人群和分子条件。", "来源标记保留综述和临床前知识，不把它们全部删除或当成无效文献。"],
        "next_steps": ["对确认有具体分子事件与临床关系的原发文献做独立核验，检查新增有效知识及旧结果回归。", "达成预定标准后才考虑将来源绑定和语境检查接入后台。"],
        "identity": {"task_version": "target-context-probe-v1",
                     "sample_versions": [read(ROOT / "manifest.json")["samples_sha256"]],
                     "method_versions": [comparison["inference_config_sha256"], context["guard_sha256"]],
                     "reference_versions": [sha(source_path), comparison["dictionary_sha256"]], "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("来源核读", source_path), ("模型和原型对照", comparison_path), ("来源语境检查", context_path), ("研究说明", doc))],
    }
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
