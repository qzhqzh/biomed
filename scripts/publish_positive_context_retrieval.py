"""Register the bounded same-article context-retrieval comparison."""

import hashlib
import json
from pathlib import Path

from apps.curation.research_history import register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/knowledge-target-optimization-20261112-v1/positive-probe-v1"
RID = "positive-context-retrieval-20261112"
GROUP = "E-42719457-0015"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish():
    old_path = ROOT / "bonsai-v1/extract" / f"result-{GROUP}.json"
    new_root = ROOT / "retrieved-context-v2"
    new_path = new_root / "bonsai-v1/extract" / f"result-{GROUP}.json"
    comparison_path, manifest_path = new_root / "comparison.json", new_root / "manifest.json"
    original, new, comparison, manifest = read(old_path), read(new_path), read(comparison_path), read(manifest_path)
    if original["status"] != "classified" or new["status"] != "classified":
        raise ValueError("Expected two complete model outputs")
    before, after = original["payload"]["facts"][0], new["payload"]["facts"][0]
    first_line = lambda fact: "first-line" in json.dumps(fact, ensure_ascii=False).casefold()
    aliases = [item["alias"] for item in manifest["additional_passages"]]
    if (first_line(before) or not first_line(after) or aliases != ["P5"]
            or manifest["additional_passages"][0]["locator"] != "body/p:9"
            or "P5" not in after["outcome"]["evidence"]
            or comparison["baseline_offline_structure_count"] != 0
            or comparison["new_offline_structure_count_including_preserved_baseline"] != 0):
        raise ValueError("Positive context outputs do not match the measured result")
    sample = read(new_root / "samples.json")[0]
    all_aliases = [(a.get("attributes") or {}).get("alias") for a in sample["record"]["anchors"]
                   if (a.get("attributes") or {}).get("alias")]
    if len(all_aliases) != len(set(all_aliases)):
        raise ValueError("Source aliases must be unique across evidence and context")
    doc, reference = PROJECT / "docs/research/034-positive-context-retrieval.md", ROOT / "source-review.json"
    record = {
        "schema_version": "research-history-v1", "id": RID,
        "title": "034 · 临床正例：同文检索补全一线治疗", "date": new["created_at"],
        "stage": "knowledge", "status": "evaluated", "task_kind": "target_compatibility",
        "sample_count": 1, "sample_unit": "条候选",
        "summary": "同一临床正例补充原文后找回一线治疗；目标结构仍0→0，尚未证明新增可用知识。",
        "task": {"question": "原始证据遗漏必要条件时，能否从同一文章找到补充段落？",
                 "input": "一条事后选择的EGFR L861Q肺癌研究候选，原有证据与同文结果段落。",
                 "outputs": ["自动找到同文补充段落并保留来源", "原模型与补充上下文后的模型输出对照", "原始目标映射缺口"],
                 "not_measured": ["Astra或临床专家核验", "多文献召回率与误收率", "真正可用的独立知识净新增"]},
        "dataset": {"label": "一条事后选择的真实临床正例", "role": "post_hoc_probe", "document_count": 1,
                    "notes": ["一条正例不能外推到所有文献。", "研究总体31人不等于L861Q亚组人数。", "初次手动试验的P4与上下文重复；保留但改用唯一P5的新版本复测。"]},
        "reference": {"label": "原文段落核读", "status": "model_reference_not_human_gold",
                      "note": "当前助手在探针输出前核读摘要及正文；非Astra或临床专家。"},
        "methods": [
            {"id": "original", "label": "原有引用段落", "version": "source-linked-clinical-facts-pilot-v1",
             "metrics": [{"label": "必要条件一线治疗", "numerator": 0, "denominator": 1, "note": "本条模型输出未保留一线。"},
                         {"label": "目标结构通过", "numerator": 0, "denominator": 1, "note": "旧实体角色和癌种别名未处理完。"},
                         {"label": "关系模型热推理", "value": original["seconds"], "unit": "秒", "note": "不含加载和其他阶段。"}],
             "conclusion": "模型认出了变异与药物，但漏掉一线治疗条件。"},
            {"id": "retrieved", "label": "自动检索补充同文段落", "version": "biomed.source-context-retrieval.v1",
             "metrics": [{"label": "必要条件一线治疗", "numerator": 1, "denominator": 1, "note": "仅一条事后诊断案例，未评估总体召回。"},
                         {"label": "目标结构通过", "numerator": 0, "denominator": 1, "note": "来源更完整，分子角色与别名映射仍待解决。"},
                         {"label": "关系模型热推理", "value": new["seconds"], "unit": "秒", "note": "不含加载和其他阶段。"}],
             "conclusion": "来源完整性在本例改善；尚未产生可计入目标的新增知识。"}],
        "findings": ["从同一文章自动找到包含L861Q、达可替尼和原结果数值的补充段落；检索命中不等于语义通过。",
                     "补充后模型保留一线治疗与24.9个月PFS，病例所属回顾性研究仍有局限。",
                     "原记录把EGFR列为上下文；癌种全名和缩写也各占一项，目标映射仍未解决。",
                     "简单把邻近基因和位点拼在一起可能把ESR1 Y537S错误归属给IGFBP3。"],
        "next_steps": ["基于原文检查基因与位点归属、癌种同义词与必要条件；对多个正负例复测后再考虑后台接入。"],
        "identity": {"task_version": "target-positive-retrieved-context-v2",
                     "sample_versions": [manifest["original_sample_sha256"], manifest["new_sample_sha256"]],
                     "method_versions": [original["config_sha256"], new["config_sha256"], sha(PROJECT / "apps/curation/source_context_retrieval.py")],
                     "reference_versions": [sha(reference), comparison["dictionary_sha256"]], "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("来源核读", reference), ("原输出", old_path), ("自动检索", manifest_path),
            ("补充后模型输出", new_path), ("目标结构对照", comparison_path), ("研究说明", doc))],
    }
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
