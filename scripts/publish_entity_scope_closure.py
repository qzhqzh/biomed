"""Register the source-reviewed entity-scope comparison in research history."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.research_history import register_record
from scripts.knowledge_observation_report import digest as snapshot_digest

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/target-identity-scope-20261112-v1"
RID = "entity-scope-closure-20261112-v3"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compatibility_preview(comp, preview):
    dictionary_path = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
    dictionary, source = read(dictionary_path), read(ROOT / "source-only.json")
    if sha(dictionary_path) != comp["dictionary_sha256"] or snapshot_digest(source) != comp["source_only_sha256"]:
        raise ValueError("Frozen source or target dictionary differs")
    articles = {str(row["pmid"]): row for row in source["articles"]}
    compared = {str(row["pmid"]): row for row in comp["records"]}
    rows = []
    for item in preview["records"]:
        pmid = str(item["pmid"])
        export = compared[pmid]["new_preview"]["derived_export"]
        companion, fact = item["biomed_companion"], item["biomed_companion"]["fact"]
        if (item["target_payload"] != export["target_payload"] or companion != export["biomed_companion"]
                or item["semantic_review_status"] != "astra_source_supported"
                or export["adaptation"]["trigger_scope"].get("clinical_context_enforced_by_target") is not False):
            raise ValueError("Reviewed candidate or clinical scope differs")
        entities = {entity["ref"]: entity for entity in companion["entities"]}
        mappings = []
        for ref, target in export["resolved_entities"].items():
            entity = entities[ref]
            mappings.append({"entity_ref": ref, "kind": entity["kind"], "label": entity["label"],
                             "roles": fact["entity_roles"].get(ref, []), "status": "来源与冻结字典对齐",
                             "target_id": target["target_id"], "target_name": target["target_name"],
                             "basis": f"冻结字典离线映射（{target['method']}）；未验证当前目标库状态。", "accepted_by_exporter": True})
        passages = {str(row["id"]): row for row in articles[pmid]["passages"]}
        quotes = []
        for evidence in item["independent_astra_assessment"]["evidence"]:
            passage = passages[str(evidence["passage_id"])]
            start = passage["text"].find(evidence["quote"])
            if start < 0 or passage["locator"] != evidence["locator"]:
                raise ValueError("Reference quote does not match frozen article")
            quotes.append({"passage_id": evidence["passage_id"], "locator": evidence["locator"],
                           "start": start, "end": start + len(evidence["quote"]), "quote": evidence["quote"]})
        article = companion["article"]
        rows.append({"id": item["group_id"], "group_id": item["group_id"], "source_run": "entity-scope-v7",
                     "candidate_id": None, "article": {key: article.get(key, "") for key in ("title", "pmid", "doi")},
                     "fact": fact, "compatibility_status": export["compatibility_status"], "offline_ready": True,
                     "quality": {"status": "Astra 原文支持（模型参考）", "reason": "Astra 独立模型参考支持核心事实；不是专家金标准，也不是患者适用性验证。"},
                     "entity_mappings": mappings, "trigger_scope": export["adaptation"]["trigger_scope"],
                     "treatment_scope": export["adaptation"]["treatment_scope"], "target_payload": item["target_payload"],
                     "mapping_issues": [{"code": "snapshot_not_covering_key", "path": "matching_validation",
                                         "detail": "冻结快照没有覆盖此逻辑键；当前目标库状态和真实位点匹配未验证。"},
                                        {"code": "clinical_context_not_enforced", "path": "trigger_scope",
                                         "detail": "一线、亚型、既往治疗或剂量等必要条件保留在文字，但目标触发字段未自动约束。"}],
                     "contract_validation": item["target_contract_validation"], "matching_validation": item["matching_validation"],
                     "live_sample_matching": "unverified", "review_points": ["结构和独立来源核验分开，当前预览不可提交。",
                                                                            "未连接目标库进行全量查重或患者匹配。"],
                     "quotes": quotes})
    source_info = dictionary["source"]
    return {"preview_only": True, "target_written": False, "dictionary": {
        "snapshot_id": source_info["snapshot_id"], "captured_at": source_info["read_at_utc"],
        "counts": {kind: len(dictionary[kind]) for kind in ("drugs", "tumors", "rules")}}, "records": rows}


def publish():
    comp_path, preview_path = ROOT / "comparison-v7.json", ROOT / "target-preview-astra-reviewed-v1.json"
    validation_path = ROOT / "astra-assessments-validation-v7.json"
    comp, preview, validation = read(comp_path), read(preview_path), read(validation_path)
    if ([comp["sample_count"], comp["baseline_offline_count"], comp["previous_preview_offline_count"],
         comp["repair_offline_count"], comp["new_offline_candidates"], comp["distinct_new_core_keys"],
         comp["not_in_known_historical_ready_keys"]] != [8, 2, 3, 4, 2, 2, 2]
            or preview["comparison_sha256"] != sha(comp_path) or preview["record_count"] != 2
            or preview["astra_source_supported_count"] != 2 or preview["submit_ready"]
            or validation["comparison_v7_sha256"] != sha(comp_path)):
        raise ValueError("Research summary does not match the frozen reviewed result")
    if {row["pmid"] for row in preview["records"]} != {"42719457", "42756523"}:
        raise ValueError("Unexpected reviewed candidates")
    manifest = read(ROOT / "manifest.json")
    doc = PROJECT / "docs/research/035-entity-scope-closure.md"
    a_ref, b_ref = ROOT / "astra-source-reference-a-exact-v2.json", ROOT / "astra-source-reference-b-exact-v2.json"
    a_review, b_review = ROOT / "astra-assessment-a-v7.json", ROOT / "astra-assessment-b-v5.json"
    target_probe, code_manifest = ROOT / "target-matcher-probes-v1.json", ROOT / "code-snapshot-v3/manifest.json"
    record = {
        "schema_version": "research-history-v1", "id": RID,
        "title": "035 · 基因位点、癌种别名与条件对照（逐条预览）",
        "date": datetime.fromtimestamp(comp_path.stat().st_mtime, timezone.utc).isoformat(),
        "stage": "knowledge", "status": "evaluated", "task_kind": "target_compatibility",
        "sample_count": 8, "sample_unit": "条候选",
        "summary": "8条定向样本：离线结构2→3→4；相对上轮多1条，2条候选经Astra原文参考支持。",
        "task": {"question": "准确归属基因位点、归并癌种别名并保留必要条件后，是否增加目标结构候选？",
                 "input": "八篇文章中定向选择的正例、反例和历史对照；同一组旧事实、文章版本和目标字典。",
                 "outputs": ["基因—位点来源关系", "癌种别名与主癌种映射", "必要条件及文章、段落两层来源", "离线目标结构与独立Astra来源核验"],
                 "not_measured": ["随机样本准确率", "生产目标库全量去重或实际位点匹配", "临床条件的自动执行"]},
        "dataset": {"label": "定向开发样本：8篇、8条", "role": "selected_development_cases", "document_count": 8,
                    "notes": ["两条正例、四条反例、两条历史对照，不能代表全库。",
                              "两条新结构候选原有事实均已存在；卵巢癌在032原型已出现。",
                              "两条核心键未出现在本次核对的5个历史已放行键中，未对全库查重。"]},
        "reference": {"label": "Astra 先看原文后核对候选", "status": "model_reference_not_human_gold",
                      "note": "两条候选核心描述有来源支持；模型参考不是临床专家金标准，也不验证生产匹配。"},
        "methods": [
            {"id": "original", "label": "原始旧候选", "version": "original-saved-candidates",
             "metrics": [{"label": "离线结构通过", "numerator": 2, "denominator": 8,
                          "note": "仅两条历史对照；结构通过不等于实际临床适配。"}],
             "conclusion": "两个诊断性正例均未形成可通过结构检查的旧导出。"},
            {"id": "prior", "label": "此前来源绑定原型", "version": "source-linked-target-preview-v1",
             "metrics": [{"label": "离线结构通过", "numerator": 3, "denominator": 8,
                          "note": "卵巢病例已通过结构，但尚缺给药频次等描述。"}],
             "conclusion": "找回卵巢癌病例；肺癌基因角色和癌种别名仍未处理。"},
            {"id": "repaired", "label": "本次来源归属与条件修复", "version": "source-linked-target-repair-v1",
             "metrics": [{"label": "离线结构通过", "numerator": 4, "denominator": 8,
                          "note": "相对上一原型新增1条；四条反例未放行。"},
                         {"label": "新增候选Astra原文支持", "numerator": 2, "denominator": 2,
                          "note": "仅核心事实；条件自动约束、真实目标ID和全库去重未验证。"},
                         {"label": "离线适配耗时", "value": comp["adaptation_seconds"], "unit": "秒",
                          "note": "8条复用已有模型输出；不含整条流水线和Astra核验。"}],
             "conclusion": "肺癌候选新增通过；卵巢癌补全给药与病例条件。"}],
        "findings": ["肺癌：EGFR L861Q、一线达可替尼、24.9个月中位PFS有原文支持；回顾性研究，药物组人数未明确。",
                     "卵巢癌：KRAS G12C、复发低级别浆液性亚型、索托拉西布960 mg口服每日一次及部分缓解为单病例描述。",
                     "相邻的IGFBP3和ESR1 Y537S不能错误拼接；11/14接受用药不能解释成缓解率。",
                     "两个新增候选只通过冻结离线合同：目标快照不覆盖相应逻辑键，一线、亚型及剂量也不会由目标触发字段自动执行。"],
        "next_steps": ["在少量未参与开发的新文章中复测，并在隔离环境验证目标库ID、去重和实际位点匹配。",
                       "明确消费者如何使用一线、亚型和给药条件；保持后台现状，验证前不导入目标生产。"],
        "identity": {"task_version": "entity-scope-closure-v7-display-v3", "sample_versions": [manifest["source_only_sha256"], manifest["candidates_sha256"]],
                     "method_versions": [comp["repair_code_sha256"], comp["gene_standard_sha256"], sha(code_manifest)],
                     "reference_versions": [comp["dictionary_sha256"], sha(a_ref), sha(b_ref), sha(a_review), sha(b_review)],
                     "completeness": "frozen"},
        "provenance": [{"label": label, "artifact": str(path), "sha256": sha(path)} for label, path in (
            ("原文与候选对照", comp_path), ("Astra核验后的离线预览", preview_path),
            ("Astra核验完整性", validation_path), ("肺癌与卵巢癌来源核验", a_review),
            ("反例及历史对照来源核验", b_review), ("只读目标匹配探针", target_probe),
            ("代码快照清单", code_manifest), ("研究说明", doc),
            ("同一实验的首版页面记录", PROJECT / "apps/curation/research_data/entity-scope-closure-20261112.json"),
            ("同一实验的前一版页面记录", PROJECT / "apps/curation/research_data/entity-scope-closure-20261112-v2.json"))],
    }
    record["target_compatibility"] = compatibility_preview(comp, preview)
    return register_record(record)


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False))
