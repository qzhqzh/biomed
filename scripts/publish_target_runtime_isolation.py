"""Publish the two-record isolated target consumer study to research history."""

import copy
import hashlib
import json
from pathlib import Path

from apps.curation.research_history import read_record, register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/target-runtime-isolation-20261112-v1"
DOC = PROJECT / "docs/research/036-target-runtime-isolation.md"
PREVIOUS = "entity-scope-closure-20261112-v3"
RID = "target-runtime-isolation-20261112"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish():
    manifest = read(ROOT / "manifest.json")
    for name, digest in manifest["inputs"].items():
        if sha(ROOT / name) != digest:
            raise ValueError("Frozen runtime artifact changed")
    identity, mode = read(ROOT / "target-identities-readonly.json"), read(ROOT / "target-treatment-mode-readonly.json")
    runtime, storage = read(ROOT / "target-runtime-synthetic.json"), read(ROOT / "target-memory-storage.json")
    context = read(ROOT / "study-context-comparison.json")
    previous = read_record(PREVIOUS)
    if (manifest["target_database_written"] or not identity["transaction_read_only"]
            or not identity["drugs_all_present"] or not identity["tumors_all_present"]
            or mode["effective_mode"] != "combined" or storage["production_written"] or storage["production_connected"]
            or len(storage["results"]) != 2 or len(runtime["results"]) != 2 or len(context["results"]) != 2):
        raise ValueError("Frozen evidence does not match the published summary")
    cases = {row["pmid"]: row for row in storage["results"]}
    other = {row["pmid"]: row for row in runtime["results"]}
    if set(cases) != {"42719457", "42756523"}:
        raise ValueError("Unexpected documents")
    for pmid, item in cases.items():
        consumer = item["consumers"]
        if (not item["serializer_valid"] or item["stored_active"] or item["stored_level"] != "-"
                or not item["exact_duplicate_rejected"] or not item["unknown_drug_id_rejected"]
                or not item["unknown_tumor_id_rejected"] or not consumer["ungraded_pending_management_text_search"]
                or consumer["inactive_patient_matches"] != 0 or consumer["simulated_approved_active_patient_matches"] != 1
                or "缺少有效" not in consumer["ungraded_report_error"]
                or set(consumer["synthetic_line_matches"].values()) != {1}
                or any(consumer["negative_counts"][name] for name in ("wrong_gene", "wrong_variant", "wrong_cancer", "missing_protein_change"))):
            raise ValueError(f"Case {pmid} differs from the measured outcome")
    if (cases["42719457"]["stored_target_resolution"]["exists"]
            or not cases["42756523"]["stored_target_resolution"]["exists"]
            or other["42719457"]["allele_index_alone_matches"]
            or not other["42719457"]["canonical_snv_self_matches"]
            or cases["42756523"]["consumers"]["negative_counts"]["annotation_gene_conflict"] != 1
            or set(cases["42756523"]["consumers"]["synthetic_ovarian_subtype_matches"].values()) != {1}):
        raise ValueError("Observed site or necessary context behavior differs")
    target_panel = copy.deepcopy(previous["target_compatibility"])
    for row in target_panel["records"]:
        case = cases[row["article"]["pmid"]]
        row["contract_validation"]["isolated_real_serializer_create"] = True
        row["contract_validation"]["isolated_pending_inactive_search"] = True
        row["contract_validation"]["isolated_exact_duplicate_rejected"] = True
        row["matching_validation"] = {"isolated_target_result": case["consumers"],
                                      "target_resolution": case["stored_target_resolution"],
                                      "note": "实际目标代码及临时内存数据库；样本合成，未验证真实患者。"}
        for mapping in row["entity_mappings"]:
            mapping["basis"] = "2026-11-12 对此 ID 进行了有限只读核对，当前名称与冻结字典一致。"
        row["mapping_issues"] = [
            {"code": "ungraded_report_incompatible", "path": "treatment_report",
             "detail": "目标允许知识未分级；模拟审核启用后，当前治疗报告要求 A/B/C/D，否则抛出错误。"},
            {"code": "clinical_context_not_enforced", "path": "disease_scope_and_trigger",
             "detail": "原文中的治疗线或亚型保留在描述中，目标匹配没有自动比较这些条件。"},
        ]
        if row["article"]["pmid"] == "42719457":
            row["mapping_issues"].append({"code": "allele_index_differs", "path": "target_resolution",
                "detail": "位点库索引为 L861，详情中的 L861Q 未解析；带完整蛋白注释的合成 SNV 可由自身精确事实命中。"})
            extra = "模拟一线与二线均命中；肺癌与非小细胞肺癌名称不会在此路径自动合并。"
        else:
            row["mapping_issues"].append({"code": "annotation_gene_conflict", "path": "synthetic_snv",
                "detail": "合成的基因字段与注释基因冲突时，既有 G12C 位点仍可导致匹配；未查询真实患者。"})
            extra = "合成的低级别浆液性及其他卵巢亚型均命中；亚型仅在文字中保留。"
        row["review_points"] = ["目标真实 serializer 在一次性内存库中完成保存；结果待处理且未启用。",
                                "模拟已审核启用、正确癌种和位点可命中；未验证真实患者。", extra,
                                "未知等级和药效不予推测；目标治疗报告当前不能直接处理未分级。"]
    record = {
        "schema_version": "research-history-v1", "id": RID, "title": "036 · 两条知识的目标运行路径隔离验证",
        "date": manifest["created_at_utc"], "stage": "knowledge", "status": "evaluated",
        "task_kind": "target_compatibility", "sample_count": 2, "sample_unit": "条知识",
        "summary": "2条未分级知识可隔离保存和检索；模拟启用后可匹配，但治疗报告报错且条件不自动约束。",
        "task": {"question": "不强行给证据分级时，两条知识能否在目标证据中心保存、检索和按原文条件使用？",
                 "input": "035中两条经Astra原文参考支持的未分级治疗知识；合成变异与上下文。",
                 "outputs": ["四个目标实体ID的有限只读核对", "目标真实serializer在内存库的保存和搜索",
                             "真实位点匹配及反例", "未分级报告行为和原文条件对照"],
                 "not_measured": ["真实患者及生产数据匹配", "真实转录本等价", "全库语义去重", "自动条件抽取准确率"]},
        "dataset": {"label": "两篇真实文章的知识与合成样本", "role": "isolated_target_consumption", "document_count": 2,
                    "notes": ["只读核对目标字典，不查询患者；保存与模拟审核启用只发生在一次性内存库。",
                              "报告模式combined为2026-11-12 15:32（北京时间）的只读快照。",
                              "原文条件的两条比较规则为本次显式指定，尚未自动抽取或接入后台。"]},
        "reference": {"label": "当前目标代码、实际serializer与SQLite隔离实测", "status": "model_reference_not_human_gold",
                      "note": "Astra原文参考沿用035；目标运行结果基于同镜像和合成样本，不是临床专家判断。"},
        "methods": [
            {"id": "identities", "label": "有限只读核对与隔离保存", "version": "target-memory-storage-v3",
             "metrics": [{"label": "目标药物和癌种 ID 存在", "numerator": 4, "denominator": 4, "note": "当前目标库有限只读查询。"},
                         {"label": "未分级保存和管理搜索", "numerator": 2, "denominator": 2, "note": "真实目标serializer及ORM；仅一次性SQLite。"},
                         {"label": "精确重复被拒绝", "numerator": 2, "denominator": 2, "note": "不代表全库语义去重。"}],
             "conclusion": "未分级知识可以作为待处理且未启用的证据信息保存和检索。"},
            {"id": "consumer", "label": "模拟目标样本匹配与报告消费", "version": "target-runtime-synthetic-v2",
             "metrics": [{"label": "模拟审核启用、正确位点命中", "numerator": 2, "denominator": 2, "note": "仅合成样本，不代表真实患者。"},
                         {"label": "错误基因、替换、主癌种及缺失蛋白变化未命中", "numerator": 8, "denominator": 8,
                          "note": "两条各4个反例；另发现KRAS基因注释冲突时仍可能命中。"},
                         {"label": "未分级治疗报告触发错误", "numerator": 2, "denominator": 2,
                          "note": "目标报告实现限制，不为通过而虚构等级。"}],
             "conclusion": "精确变异可命中；当前报告不能直接消费未分级治疗证据。"},
            {"id": "context", "label": "来源条件的独立比较原型", "version": "target-context-comparison-v1",
             "metrics": [{"label": "已知相同范围", "numerator": 2, "denominator": 2, "note": "一线治疗与卵巢亚型两条合成对照。"},
                         {"label": "已知不同范围", "numerator": 2, "denominator": 2, "note": "目标自身均命中；原型仅标出与文献范围不同，不隐藏知识。"},
                         {"label": "缺少范围信息", "numerator": 2, "denominator": 2, "note": "输出未知，不自动判患者不适用。"}],
             "conclusion": "比较规则可表达一致、不同和未知；条件自动抽取及目标接入尚未完成。"}],
        "findings": ["EGFR位点库索引为L861而非L861Q：目标详情标未解析，完整蛋白注释的合成SNV仍可由自身精确事实命中。",
                     "目标管理端可以检索未分级、未启用的证据；模拟启用后，当前治疗报告对未分级抛错。",
                     "目标匹配不区分一线、二线或卵巢癌亚型；原文条件和文章来源保留在描述，段落精确定位依赖biomed。",
                     "KRAS的合成输入若样本基因与注释基因冲突，现有位点库仍可能导致命中；没有查询真实患者。"],
        "next_steps": ["保留未分级知识并供信息检索，不编造证据等级或药物敏感性。",
                       "设计目标消费者对未分级、未知药效和原文范围的展示与处理；验证前不启用生产报告匹配。",
                       "在新文章中验证条件的自动提取与标准化，缺失时保留未知，不阻止知识检索。"],
        "identity": {"task_version": "target-runtime-isolation-v1", "sample_versions": [manifest["preview_sha256"], sha(ROOT / "target-identities-readonly.json")],
                     "method_versions": [sha(ROOT / "target-memory-storage.json"), sha(ROOT / "study-context-comparison.json"),
                                         manifest["code"]["apps/curation/evidence_scope_context.py"]],
                     "reference_versions": [sha(ROOT / "target-treatment-mode-readonly.json"), sha(ROOT / "target-runtime-synthetic.json")],
                     "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("四个ID与位点只读核对", ROOT / "target-identities-readonly.json"),
            ("报告模式只读快照", ROOT / "target-treatment-mode-readonly.json"),
            ("真实目标保存与消费者隔离结果", ROOT / "target-memory-storage.json"),
            ("数据库连接禁用的合成对照", ROOT / "target-runtime-synthetic.json"),
            ("原文范围对照", ROOT / "study-context-comparison.json"), ("实验清单", ROOT / "manifest.json"),
            ("研究说明", DOC))], "target_compatibility": target_panel,
    }
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
