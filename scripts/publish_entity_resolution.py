"""Publish the completed, frozen entity comparison; no inference or target access."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.research_history import prepare_record, register_record

RID = "entity-resolution-20261101"
BUSINESS_RID = "entity-resolution-business-20261101-v2"
ROOT = PROJECT / "var/experiments" / RID
LABELS = {"qwen35-9b": "Qwen3.5-9B", "bonsai2-27b": "Bonsai 2 27B"}
RELATIONS = {"exact_identity": "同一身份", "alias": "别名", "qualified_form": "核心疾病＋条件", "new_entity": "新增", "unresolved": "待定", "not_entity": "非本次实体范围"}


def make_record(root=ROOT):
    provenance = []

    def load(relative):
        path = root / relative
        raw = path.read_bytes()
        provenance.append({"label": relative, "artifact": f"var/experiments/{root.name}/{relative}",
                           "sha256": hashlib.sha256(raw).hexdigest()})
        return json.loads(raw)

    report = load("comparison.json")
    manifest = load("manifest.json")
    reference = load("reference.json")
    inputs = load("qwen35-9b/inputs.json")
    if load("bonsai2-27b/inputs.json") != inputs:
        raise ValueError("Both models must use identical inputs")
    plan = load("evaluation-plan.json")
    if not (report["reference_sha256"] == plan["reference_sha256"] == digest(reference)
            and reference["inputs_sha256"] == plan["inputs_sha256"] == digest(inputs)):
        raise ValueError("Input/reference binding differs from the executed plan")
    items = load("items.json")["records"]
    catalog = {row["id"]: row for row in load("candidate-catalog.json")["records"]}
    for item in items:
        for candidate in item["candidates"] + item["main_cancer_candidates"]:
            catalog.setdefault(candidate["id"], candidate)
    refs = {row["id"]: row for row in reference["records"]}
    if len(items) != 20 or sum(row["cohort"] == "unresolved_after_rules" for row in items) != 19:
        raise ValueError("Expected 19 operational terms and one independent challenge")
    arms = report["arms"]
    selected = report["selection"]["model"]
    results = {arm: {row["id"]: row for row in value["records"]} for arm, value in arms.items()}
    if any(set(rows) != set(refs) for rows in results.values()):
        raise ValueError("Comparison must cover all frozen references")

    def entity_name(identifier):
        if not identifier:
            return ""
        item = catalog.get(identifier, {})
        return item.get("name_cn") or item.get("label") or item.get("name") or identifier

    def outcome_text(value):
        parts = [entity_name(value.get("candidate_id"))]
        if value.get("main_cancer_id"):
            parts.append("主癌种：" + entity_name(value["main_cancer_id"]))
        if value.get("subtype_candidate_id"):
            parts.append("亚型：" + entity_name(value["subtype_candidate_id"]))
        parts.append(RELATIONS.get(value.get("match_relation"), ""))
        return "；".join(part for part in parts if part)

    cases = []
    for item in items:
        expected = refs[item["id"]]
        answer = expected["accepted"][0]
        methods = []
        for arm, rows in results.items():
            row = rows[item["id"]]
            prediction = row["prediction"]
            failures = [label for key, label in [("identity_correct", "身份未通过"),
                        ("classification_correct", "层级未通过"), ("qualifiers_correct", "必要限定未通过"),
                        ("protocol_valid", "出处／输出格式未通过")] if not row[key]]
            methods.append({"id": arm, "label": LABELS[arm], "decision": prediction.get("decision", "未完成"),
                            "entity": outcome_text(prediction), "correct": row["all_correct"],
                            "seconds": row["seconds"], "reason": prediction.get("reason", "")
                            + (" 核对：" + "、".join(failures) if failures else "")})
        cases.append({"id": item["id"], "kind": item["kind"], "label": item["label"], "cohort": item["cohort"],
                      "reference_note": expected["reason"], "reference_decision": answer["decision"],
                      "reference_entity": outcome_text(answer),
                      "required_qualifiers": [" / ".join(group) for group in expected.get("required_qualifiers", [])],
                      "quotes": expected.get("source_quotes", []), "methods": methods})

    methods, versions = [], []
    for arm, value in arms.items():
        definition = load(f"{arm}/definition.json")
        versions.append(f"{arm}:{value['inference_summary'].get('config_sha256') or digest(definition)}")
        score = value["cohorts"]["unresolved_after_rules"]
        challenge = value["cohorts"]["known_risky_synonym_challenge"]
        operational = [row for row in value["records"] if row["cohort"] == "unresolved_after_rules"]
        seconds = round(sum(row["seconds"] or 0 for row in operational), 3)
        metrics = [{"label": label, "numerator": score[key], "denominator": score["items"], "note": note}
                   for key, label, note in [
                       ("identity_correct", "真实待解原词：身份与对应关系", "不把核心疾病与带限定疾病视为无条件同义词。"),
                       ("classification_correct", "真实待解原词：主癌种／亚型", "按冻结的业务主癌种与亚型参考判断。"),
                       ("qualifiers_correct", "真实待解原词：必要限定", "要求保留参考规定的必要限定，并有输入原文依据。"),
                       ("all_correct", "真实待解原词：完整结果通过", "身份、层级、必要限定、原文与输出格式同时通过。")]]
        metrics.extend([
            {"label": "真实待解原词：错误合并", "value": f"{score['false_merge']} / {score['items']}", "note": "错误地选中不应合并的已有实体，越少越好。"},
            {"label": "真实待解原词：遗漏已有身份", "value": f"{score['missed_existing_identity']} / {score['items']}", "note": "有明确对应却回答新增或待定，越少越好。"},
            {"label": "完整通过中正确保留待定", "value": sum(row["all_correct"] and row["prediction"].get("decision") == "UNRESOLVED" for row in operational),
             "unit": "项", "note": "不确定时保留待定可以是正确答案；这些项仍须补充依据，不计作已完成实体对应。"},
            {"label": "真实19项暖推理总耗时", "value": seconds, "unit": "秒", "note": "只统计这19项请求，不包含模型加载和独立挑战。"},
            {"label": "独立DMG挑战：完整结果", "value": f"{challenge['all_correct']} / {challenge['items']}", "note": "单独挑战已知风险同义词，不计入真实待解工作量。"},
        ])
        inference = value["inference_summary"]
        if inference.get("nvml_process_peak_mib") is not None:
            metrics.append({"label": "推理进程峰值显存", "value": round(inference["nvml_process_peak_mib"] / 1024, 2),
                            "unit": "GiB", "note": "本次完整20项运行的NVML采样峰值；两模型精度／量化及运行时不同，不能只按参数规模解释。"})
        methods.append({"id": arm, "label": LABELS[arm], "version": versions[-1], "metrics": metrics,
                        "conclusion": "本阶段歧义处理选用此模型。" if arm == selected else "保留实测对照，本阶段不设逐项挑选最优模型的流程。"})

    summary = f"30条源知识中29条有本轮药物／疾病参与者，共89处引用，规则先处理{manifest['rule_resolved_mentions']}处；19个真实待解原词对比9B与Bonsai，另列1个DMG挑战。本阶段选择{LABELS[selected]}处理歧义。"
    return prepare_record({
        "schema_version": "research-history-v1", "id": RID,
        "title": "023 · 本地实体库、名称对齐与歧义模型比较", "date": datetime.fromtimestamp((root / "comparison.json").stat().st_mtime, timezone.utc).isoformat(),
        "stage": "entities", "status": "evaluated", "task_kind": "entity_resolution",
        "sample_count": 20, "sample_unit": "项（19真实原词＋1独立挑战）", "summary": summary,
        "task": {"question": "已有知识中的药物、癌种／疾病名称应对应哪个本地实体；何时新增、保留待定或保留亚型与限定？",
                 "input": "相同的19个实际歧义原词、来源片段和冻结候选字典；另用1个DMG风险同义词作独立挑战。",
                 "outputs": ["实体身份对应或新增／待定", "主癌种、亚型与必要限定", "可回溯依据和可复用的本地规则"],
                 "not_measured": ["全库泛化准确率与客户部署表现", "tumor_api真实导入与报告匹配", "模型训练或新增知识语义核验"]},
        "dataset": {"label": "现有30条知识中的实体名称开发集", "role": "development",
                    "notes": ["19个真实待解原词对应33处知识引用；规则处理29个唯一原词、56处引用。",
                              "DMG挑战是另外添加的已知风险项，不是第20个实际待解原词，也不参与运行模型选型排名。",
                              "基础实体底本一次性保存；本实验未在线访问tumor_api。"]},
        "reference": {"label": "推理前冻结的原文与字典参考", "status": "model_reference_not_human_gold",
                      "note": "参考先于模型输出冻结；接受明确列出的等价身份形式，同时检查必要限定。这是开发集参考，不是临床验证。"},
        "methods": methods,
        "findings": ["规则与字典先处理确定项，模型仅处理实际歧义；模型调用次数不等同于知识引用数。",
                     "完整通过包含正确保留未决的答案，不等于所有原词都已完成对应。",
                     "本次9B身份正确12/19、错合并0；Bonsai身份正确11/19、错合并2，但完整结果11/19高于9B的9/19且更快。依预定身份及错合并优先规则暂选9B，不能称其所有维度更好。",
                     "亚型属于主癌种，与同一实体别名是两种关系。qualified_form表示核心疾病加条件，条件不可在归一时抹去。",
                     "按真实19项的身份正确、错误合并、完整结果、层级及耗时顺序选择模型；不按每一项事后挑最好答案。",
                     "研究参考通过可登记为AI依据；模型来源与人工审核分别保留。不能把本地对齐成功当作目标证据中心导入就绪。"],
        "next_steps": ["在实体库页面核对剩余歧义及新增实体，决定按名称和适用范围复用。",
                       "后续文献仅查本地库，确实出现未登记实体时扩展；稳定后不再拉取tumor_api。"],
        "entity_resolution": {"cases": cases},
        "identity": {"task_version": report["version"], "sample_versions": [digest(inputs)],
                     "method_versions": versions, "reference_versions": [digest(reference)], "completeness": "frozen"},
        "provenance": provenance,
    })


def make_business_record(root=ROOT):
    """Append the user's revised acceptance without rewriting strict history."""
    record = make_record(root)
    record.pop("fingerprint")
    record.pop("content_sha256")
    path = root / "comparison-business-v2.json"
    raw = path.read_bytes()
    report = json.loads(raw)
    if not report.get("no_new_inference") or not report.get("post_hoc_acceptance_change"):
        raise ValueError("Business view requires a declared reassessment, not new inference")
    source_files = {"comparison_sha256": "comparison.json", "reference_sha256": "reference.json",
                    "inputs_sha256": "qwen35-9b/inputs.json", "items_sha256": "items.json"}
    for key, filename in source_files.items():
        if report["provenance"][key] != digest(json.loads((root / filename).read_bytes())):
            raise ValueError(f"Business reassessment changed source binding: {filename}")
    if not report["provenance"].get("original_outputs_verified") or report["provenance"].get("original_artifacts_rewritten"):
        raise ValueError("Original model outputs must be verified and preserved")
    catalog = {row["id"]: row for row in json.loads((root / "candidate-catalog.json").read_bytes())["records"]}
    for item in json.loads((root / "items.json").read_bytes())["records"]:
        for candidate in item["candidates"] + item["main_cancer_candidates"]:
            catalog.setdefault(candidate["id"], candidate)

    def entity_name(identifier):
        candidate = catalog.get(identifier, {})
        return candidate.get("name_cn") or candidate.get("label") or identifier or "未确定"

    selected = report["selection"]["model"]
    methods = []
    business_rows = {}
    for arm in LABELS:
        value = report["arms"][arm]
        disease, drugs, challenge = value["disease"], value["drugs"], value["challenge"]
        if (disease["items"], drugs["items"], challenge["items"]) != (14, 5, 1):
            raise ValueError("Business disease, drug and challenge denominators changed")
        business_rows[arm] = {row["id"]: row for group in (disease, drugs, challenge) for row in group["records"]}
        extension = disease["extended_fields"]
        matched_drugs = sum(row["identity_correct"] and row["prediction"].get("decision") == "MATCH" for row in drugs["records"])
        undetermined_drugs = sum(row["identity_correct"] and row["prediction"].get("decision") == "UNRESOLVED" for row in drugs["records"])
        metrics = [
            {"label": "疾病：主癌种字段直接正确", "numerator": disease["direct_main_correct"], "denominator": 14,
             "note": "主要指标。主类正确即可；不要求亚型、分期或限定同时完整。解释提及但主类字段为空，单列待组装。"},
            {"label": "疾病：本地字典回填后主类正确", "numerator": disease["accepted_with_recovery"], "denominator": 14,
             "note": f"包含直接正确项；本次额外回填 {disease['script_recovered']} 项。只用原输出实体候选与冻结层级映射，不从解释补答案。"},
            {"label": "疾病：解释已识别、字段待组装", "value": disease["explanation_only_main_id_mentions"], "unit": "项",
             "note": "不属于医学概念认错；没有计入直接可用主类字段。"},
            {"label": "药物：判断符合参考", "numerator": drugs["identity_correct"], "denominator": 5,
             "note": f"药物单独计分。其中明确匹配成分 {matched_drugs} 项，正确保留未决 {undetermined_drugs} 项；未决不等于已找到药物。"},
            {"label": "药物：明确匹配具体成分", "value": f"{matched_drugs} / 5", "note": "只计选对已有具体药物的MATCH结果。"},
            {"label": "独立DMG挑战：主癌种字段正确", "value": f"{challenge['direct_main_correct']} / 1", "note": "挑战单列，不纳入14个实际疾病原词。"},
            {"label": "疾病：独立扩展字段填报完整", "numerator": extension["extension_fields_complete"], "denominator": 14,
             "note": "独立亚型字段、必要限定和出处格式的填报状态；candidate_id已表达亚型但独立字段缺填仍标待组装，不代表未识别亚型，不否决主类正确。"},
        ]
        for key, label in [("subtype_matches_reference", "亚型"), ("required_qualifiers_present", "限定")]:
            denominator = extension[key + "_applicable"]
            if denominator:
                metrics.append({"label": "独立亚型字段已填写且符合参考" if key == "subtype_matches_reference" else "限定字段已填写且符合参考",
                                "numerator": extension[key + "_applicable_correct"], "denominator": denominator,
                                "note": f"仅以确有{label}字段要求的原词为分母，不影响主类正确。" + ("candidate_id中已有亚型身份与此重复字段的填报分开评价，不能称作亚型识别率。" if key == "subtype_matches_reference" else "")})
        resources = value["resources"]
        metrics.extend([
            {"label": "原19项暖推理耗时", "value": round(resources["disease_warm_seconds"] + resources["drug_warm_seconds"], 3), "unit": "秒",
             "note": "复用原测量：14疾病＋5药物，不含独立挑战、加载与预热；此次没有再次推理。"},
            {"label": "原运行进程峰值显存", "value": round(resources["nvml_process_peak_mib"] / 1024, 2), "unit": "GiB",
             "note": "复用原20项测量；两模型量化／运行时不同。"},
        ])
        original_method = next(method for method in record["methods"] if method["id"] == arm)
        methods.append({**original_method, "metrics": metrics, "conclusion": "按当前主类业务口径，建议用于本环节的实体归一歧义处理。" if arm == selected else "保留对照；主类字段问题与扩展信息完整度分别处理。"})

    for case in record["entity_resolution"]["cases"]:
        disease_case = case["kind"] == "disease"
        first = business_rows[next(iter(LABELS))][case["id"]]
        if disease_case:
            case["reference_main"] = entity_name(first["expected_main_id"])
            case["reference_note"] = "本次核心判断只看主癌种；扩展字段填报另列。独立亚型字段缺填不代表未识别亚型，candidate_id中已有的亚型身份仍保留。"
        for method in case["methods"]:
            row = business_rows[method["id"]][case["id"]]
            method["reason"] = row["prediction"].get("reason", "")
            if disease_case:
                extension = row["extended_fields"]
                method["correct"] = row["business_correct"]
                method["core_result"] = {"direct": row["direct_main_correct"], "recovered": row["script_recovered"],
                                         "accepted": row["business_correct"], "explanation_only": row["explanation_only_main_id_mention"]}
                method["completeness"] = [{"label": "出处／格式", "correct": extension["source_protocol_valid"]}]
                if extension["subtype_required"]:
                    method["completeness"].append({"label": "独立亚型字段", "correct": extension["subtype_matches_reference"]})
                if extension["qualifier_requirement_present"]:
                    method["completeness"].append({"label": "限定", "correct": extension["required_qualifiers_present"]})
                if row["explanation_only_main_id_mention"]:
                    method["reason"] += " 业务核对：解释已识别正确主类，结构字段待组装；不按医学误认处理。"
            else:
                method["correct"] = row["identity_correct"]
                method["completeness"] = [{"label": "出处／格式", "correct": row["protocol_valid"]},
                                          {"label": "原扩展字段", "correct": row["classification_correct"] and row["qualifiers_correct"]}]

    qwen = report["arms"]["qwen35-9b"]
    bonsai = report["arms"]["bonsai2-27b"]
    record.update(
        id=BUSINESS_RID, title="023 · 癌种主类业务口径（v2）",
        date=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        sample_unit="项（14疾病＋5药物＋1独立挑战）",
        summary=f"按用户定义重评原输出：14个疾病中，9B主类字段直接正确{qwen['disease']['direct_main_correct']}/14，Bonsai {bonsai['disease']['direct_main_correct']}/14；药物5项单列。字段完整度不否决主类正确，本环节建议{LABELS[selected]}。",
        task={"question": "癌种是否归到正确主类（如前列腺癌）；药物判断是否正确？",
              "input": "复用原19项真实输出：14个疾病、5个药物；另有1个DMG挑战。此次修改验收口径，没有重新调用模型。",
              "outputs": ["主癌种字段正确且可用；字典回填另列", "药物明确匹配与正确保留未决分开", "亚型、分期／限定和出处字段完整度独立显示"],
              "not_measured": ["新样本泛化或新推理性能", "改变实体库确认状态、原知识或生产导入", "之前9B从原文抽取实体任务的选型"]},
        methods=methods,
        reference={"label": "用户确认的主类业务口径＋原冻结参考", "status": "model_reference_not_human_gold",
                   "note": "这是看到原输出后，按用户澄清的验收目标重新评分；非新的盲测。仅改计分，原模型输出和历史严格记录保留。"},
        findings=["主癌种对即可判核心正确，亚型、分期和限定缺失另记完整度，不再将所有字段捆成一个对错。",
                  "旧12/19、11/19混合了药物与疾病并同时检查关系标签等字段，仅是历史混合指标，不能当作癌种主类正确率。",
                  "9B的E011解释已识别小细胞肺癌，但主类字段为空：属于字段组装问题，不泛称医学认错。",
                  "Bonsai药物判断3/5包含2个正确保留未决；两模型明确匹配具体成分都只有1项。",
                  f"本环节按新业务口径建议{LABELS[selected]}处理实体归一歧义；不改变之前9B原文实体抽取的选型，也不因此确认原有待审规则。"],
        next_steps=["后续按主类可用性与字段完整度分别核对；只处理实际出现的缺口。", "药物候选同义词和药物专用判断仍需独立完善，未决药物不自动录入。"],
    )
    record["dataset"]["notes"] = ["14个实际疾病原词为主类计分分母，5个实际药物单独计分。",
                                      "DMG挑战独立列示，不纳入14个真实疾病原词。",
                                      "后验口径变更；原输入、输出、参考与时间／显存测量不改写。"]
    record["entity_resolution"].update(scoring="business-v2", previous_record=RID)
    record["identity"]["task_version"] = report["version"]
    record["identity"]["reference_versions"] += ["business-policy:" + digest(report["policy"]),
                                                  "candidate-main-map:" + digest(report["explicit_candidate_main_map"])]
    record["provenance"].append({"label": "用户业务口径重评", "artifact": f"var/experiments/{root.name}/{path.name}",
                                  "sha256": hashlib.sha256(raw).hexdigest()})
    return prepare_record(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--business", action="store_true", help="Append the user's revised main-cancer acceptance as v2")
    args = parser.parse_args()
    record = make_business_record(args.root) if args.business else make_record(args.root)
    result = register_record(record) if args.apply else {"id": record["id"], "summary": record["summary"], "methods": record["methods"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
