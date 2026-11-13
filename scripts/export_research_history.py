"""Register reviewed, public research summaries without rerunning inference.

Raw inputs remain in ignored experiment directories. This exporter projects only
the reviewed fields below; it never copies complete manifests, raw model output,
absolute source paths, private credentials, or full papers into the website.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPERIMENTS = ROOT / "var/experiments"
CASE_FIELDS = (
    "cohort", "rank", "pmid", "title", "source_scope", "classification_input_scope",
    "reference_state", "reference_relevance", "reference_scope", "reference_notes",
    "acceptable_relevance", "scope_eligible", "rule_scope", "model_scope",
    "model_relevance", "model_status", "model_error", "model_seconds",
    "rule_outcome", "model_outcome", "model_relevance_outcome",
    "rule_acceptable_scope", "model_acceptable_scope", "model_acceptable_relevance",
)
QUOTE_FIELDS = ("field", "text", "start", "end")
LABEL_FIELDS = ("axis", "id", "label", "role")
COHORT_LABELS = {
    "evidence-pilot-20261022-first20": "最早入库 20 篇",
    "evidence-pilot-20261022-solid10": "实体瘤补充 10 篇",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(path: Path, label: str) -> dict:
    return {"label": label, "artifact": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def fraction(label: str, numerator: int, denominator: int, note: str) -> dict:
    return {"label": label, "numerator": numerator, "denominator": denominator, "note": note}


def value(label: str, amount: float | str, note: str, unit: str = "") -> dict:
    return {"label": label, "value": amount, "unit": unit, "note": note}


def base_record(record_id: str, title: str, date: str, sample_count: int) -> dict:
    return {
        "schema_version": "research-history-v1", "id": record_id, "title": title,
        "date": date, "stage": "classification", "status": "evaluated",
        "sample_count": sample_count,
    }


def historical_records() -> list[dict]:
    evaluation_doc = ROOT / "docs/literature-evaluation.md"
    model_doc = ROOT / "docs/literature-model-pilot.md"
    reference = {
        "label": "助手逐篇暂定核读参考", "status": "assistant_reviewed_provisional",
        "note": "非人工金标准；没有第二位独立标注者，不能解释为临床准确率。",
    }
    task = {
        "question": "文章实际研究对象属于哪个肿瘤范围？",
        "input": "已保存的标题、摘要、MeSH 和出版类型",
        "outputs": ["实体瘤", "血液肿瘤", "实体/血液混合", "实体/良性或癌前边界", "良性", "癌前线索", "待判定"],
        "not_measured": ["癌种/器官等多标签轴的准确率", "全文证据抽取质量", "临床有效性"],
    }
    rules = base_record("rules-review-20261016", "规则 v2.1 → v3：开发集改善，保留集提升有限", "2026-10-16", 100)
    rules.update({
        "summary": "固定 60 篇开发、40 篇保留样本；开发集一致数 46→58，保留集 29→30。不能将开发集成绩外推到全库。",
        "task": task,
        "dataset": {"label": "100 篇分层抽样（60 开发 + 40 保留）", "role": "development_and_historical_holdout", "notes": [
            "来自当时 1,000 篇近期采集，有意富集困难条目，不代表总体分布。",
            "覆盖原 288 篇待判定中的 50 篇，另 238 篇当时未逐篇核读。",
            "保留集在规则冻结后核读；后续依据它改规则时须转为开发材料。",
        ]},
        "reference": reference,
        "methods": [
            {"id": "rules_v21", "label": "规则 v2.1", "version": "solid-tumor-rules-v2.1", "metrics": [
                fraction("开发集范围一致", 46, 60, "用于后续规则修订，不能视作泛化表现。"),
                fraction("保留集范围一致", 29, 40, "包含参考为待判定的样本。"),
                fraction("保留集明确范围一致", 17, 26, "排除参考仍为待判定的样本。"),
            ], "conclusion": "作为改动前基线保留，未覆盖旧版本。"},
            {"id": "rules_v3", "label": "规则 v3", "version": "solid-tumor-rules-v3", "metrics": [
                fraction("开发集范围一致", 58, 60, "开发集用于规则修订。"),
                fraction("保留集范围一致", 30, 40, "仅多一致 1 篇，不能证明稳定提升。"),
                fraction("保留集明确范围一致", 18, 26, "未将正确保留待判定算入此分母。"),
            ], "conclusion": "补癌种表达并排除分子名称、正常对照伪命中；背景、既往史与对象关系仍有误判。"},
        ],
        "findings": [
            "50 篇原待判定核读：12 篇实体瘤、25 篇泛癌类型未限定、12 篇主要非肿瘤、1 篇信息不足。",
            "少数类别样本很少；没有证明所有癌种、血液及良性边界均可靠。",
            "另核对的 29 条发表状态提示属于独立富集队列，不能合并到上述分母。",
        ],
        "next_steps": ["保留错误原因与版本；后续分类任务先明确定义及分母，再使用新的未见样本验证。"],
        "identity": {"task_version": "solid-tumor-annotation-v1", "sample_versions": ["documented:solid-tumor-eval-v1/development60+holdout40"], "method_versions": ["solid-tumor-rules-v2.1", "solid-tumor-rules-v3"], "reference_versions": ["assistant_reviewed_provisional"], "completeness": "documented_only"},
        "provenance": [provenance(evaluation_doc, "历史核读、规则版本与分组指标")],
    })
    results = [rules]
    for version, profile, correct, valid, seconds, mean, peak in (
        ("v1", "multi_axis", 6, 17, 201.60, 10.08, 3687.4),
        ("v2", "relevance", 0, 1, 155.51, 7.78, 3587.4),
    ):
        multi = version == "v1"
        record = base_record(
            "qwen-multi-axis-20261016" if multi else "qwen-relevance-20261016",
            "Qwen3-1.7B v1：范围与多标签联合输出" if multi else "Qwen3-1.7B v2：收窄对象范围仍有格式失败",
            "2026-10-16", 20,
        )
        record.update({
            "summary": f"同一组开发样本 20 篇，整份输出合格且范围一致 {correct}/20，结构及引句校验通过 {valid}/20；没有训练。",
            "task": {**task, "outputs": task["outputs"] + (["器官、组织学、研究类型、主题、人群候选标签"] if multi else [])},
            "dataset": {"label": "原 60 篇开发集的 5 个抽样层各取 4 篇", "role": "development", "notes": [
                "这是 2026-10-16 的分层 20 篇，不是 2026-10-22 的最早入库 20 篇。",
                "v1、v2 使用同一组样本；v2 根据 v1 错误修改提示词，不是独立测试。",
            ]},
            "reference": reference,
            "methods": [
                {"id": "rules", "label": "同样本规则 v3", "version": "solid-tumor-rules-v3", "metrics": [fraction("范围一致", 19, 20, "开发样本及助手暂定参考，不代表全库正确率。")], "conclusion": "保留为相同样本基线；本轮未单独测速。"},
                {"id": "qwen", "label": f"Qwen3-1.7B {version}", "version": f"qwen3-1.7b-pilot-{version} / {profile}", "metrics": [
                    fraction("整份合格且范围一致", correct, 20, "输出失败保留在分母，不能解释为所有原始文字判断都错。"),
                    fraction("结构及引句校验通过", valid, 20, "仅协议通过率，不是语义正确率。"),
                    value("推理总耗时", seconds, "不含加载、排队和参考标注。", "秒"),
                    value("平均推理耗时", mean, "包含失败输出的 20 次尝试，不是端到端吞吐。", "秒/篇"),
                    value("峰值显存分配", peak, "物理 GPU 3，FP16，仅本地推理。", "MiB"),
                ], "conclusion": "16 篇输出泛癌类型未定；3 份输出因非法标签/角色或引句不合格被拒。" if multi else "17 份缺少必需结构、2 份多余 JSON/文本；唯一合格输出仍未与参考一致。"},
            ],
            "findings": [
                "使用现成 Qwen3-1.7B，没有领域后训练；此配置不能替代默认规则。",
                "多标签轴没有参考，不能把主范围分数解释为器官或所有字段都错。",
                "2026-10-22 的运行库与本轮不同，跨日期结果不是严格单变量对照。",
            ],
            "next_steps": ["拆开主分类与附加标签校验；先查提示词、解码与格式约束，再讨论后训练。"],
            "identity": {"task_version": "solid-tumor-decision-v1", "sample_versions": ["documented:solid-tumor-eval-v1/development-stratified20"], "method_versions": [f"qwen3-1.7b-pilot-{version}", profile, "solid-tumor-rules-v3"], "reference_versions": ["assistant_reviewed_provisional"], "completeness": "documented_only"},
            "provenance": [provenance(model_doc, "历史模型配置、同样本对照与失败原因")],
        })
        results.append(record)
    return results


def public_case(record: dict) -> dict:
    projected = {key: record[key] for key in CASE_FIELDS if key in record}
    projected["reference_quotes"] = [
        {key: quote[key] for key in QUOTE_FIELDS if key in quote}
        for quote in record.get("reference_quotes", [])
    ]
    for axis in ("reference_labels", "rule_labels", "model_labels"):
        projected[axis] = [
            {
                **{key: label[key] for key in LABEL_FIELDS if key in label},
                "matches": [{key: quote[key] for key in QUOTE_FIELDS if key in quote} for quote in label.get("matches", [])],
            }
            for label in record.get(axis, [])
        ]
    return projected


def current_records() -> list[dict]:
    evaluation_path = EXPERIMENTS / "evidence-pilot-20261022-comparison/evaluation.json"
    evaluation = read_json(evaluation_path)
    if evaluation.get("schema_version") != "evidence-pilot-evaluation-v1":
        raise ValueError("Unexpected source evaluation schema")
    if len(evaluation["records"]) != 30 or len(evaluation["cohorts"]) != 2:
        raise ValueError("This reviewed export requires the fixed 20 + 10 experiment")
    cases, raw_counts, diagnostic_matches, diagnostic_total = [], Counter(), 0, 0
    sources = [provenance(evaluation_path, "冻结样本对照评测")]
    sample_versions, reference_versions, method_versions = [], [], []
    for source in evaluation["cohort_sources"]:
        folder = EXPERIMENTS / source["cohort"]
        for name, key in (("snapshot.json", "snapshot_file_sha256"), ("astra-reference-v1.json", "reference_sha256")):
            if sha256(folder / name) != source[key]:
                raise ValueError(f"Frozen source changed: {source['cohort']}/{name}")
            sources.append(provenance(folder / name, f"{COHORT_LABELS[source['cohort']]} {name}"))
        sample_versions.append(source["snapshot_sha256"])
        reference_versions.append(source["reference_sha256"])
    for item in evaluation["records"]:
        # Resolve known source identifiers, never a private path from evaluation.json.
        if item["cohort"] not in COHORT_LABELS or not str(item["pmid"]).isdigit():
            raise ValueError("Unexpected cohort or PMID")
        path = EXPERIMENTS / item["cohort"] / "model" / f"result-{item['pmid']}.json"
        result = read_json(path)
        raw = json.loads(result["raw_output"])
        raw_relevance = raw.get("relevance")
        if not isinstance(raw_relevance, str):
            raise TypeError("Raw relevance is missing; do not silently omit failed cases")
        raw_counts[raw_relevance] += 1
        if item["reference_state"] == "determinate":
            diagnostic_total += 1
            diagnostic_matches += raw_relevance == item["reference_relevance"]
        case = public_case(item)
        case["model_raw_relevance"] = raw_relevance
        if item["model_status"] != "validated_candidate":
            case["model_raw_relevance_note"] = "事后读取原始 JSON 的主类；整份输出未通过校验，不是合格模型结果，也未重新推理。"
        cases.append(case)
        sources.append(provenance(path, f"PMID {item['pmid']} 原始输出诊断来源"))
    if raw_counts != Counter({"oncology_unspecified": 29, "non_neoplastic": 1}) or (diagnostic_matches, diagnostic_total) != (1, 28):
        raise ValueError("Raw diagnostics changed; require a new reviewed research record")
    methods = []
    for cohort in evaluation["cohorts"]:
        folder = EXPERIMENTS / cohort["id"]
        manifest_path = folder / "model/manifest.json"
        sources.append(provenance(manifest_path, f"{COHORT_LABELS[cohort['id']]} 模型配置"))
        for method in cohort["methods"]:
            local = method["id"] == "local_model"
            version = method["config_sha256"] if local else method["taxonomy_sha256"]
            method_versions.append(version)
            if local and sha256(manifest_path) != method["manifest_sha256"]:
                raise ValueError("Model manifest no longer matches evaluation")
            metrics = [
                fraction("明确范围一致", method["matched"], method["denominator"], "仅参考范围明确的样本；泛癌/非肿瘤/信息不足映射到旧 scope 的待判定，不能作为正确非肿瘤计分。"),
                value("保留待判定", method["abstained"], "在上述明确范围分母内，模型未给出具体范围。", "篇"),
                value("输出失败", method["failed"], "在上述明确范围分母内；未丢弃失败样本。", "篇"),
            ]
            if local:
                metrics.append(value("平均推理耗时", round(method["mean_seconds"], 2), "本组所有尝试，含失败；不含模型加载、参考核读和导出。", "秒/篇"))
            methods.append({
                "id": f"{method['id']}_{cohort['id'].rsplit('-', 1)[1]}",
                "label": f"{COHORT_LABELS[cohort['id']]} · {'Qwen3-1.7B' if local else '规则 v3'}",
                "version": method["version"] + (" / multi_axis" if local else ""), "metrics": metrics,
                "conclusion": "合格输出全部为泛癌类型未定，不能用于分流明确实体或血液肿瘤。" if local else "同一参考下的开发样本范围基线；不代表全库准确率。",
            })
    statuses = Counter(item["model_status"] for item in cases)
    matched = sum(item["model_relevance"]["matched"] for item in evaluation["cohorts"])
    denominator = sum(item["model_relevance"]["denominator"] for item in evaluation["cohorts"])
    seconds = sum(item["model_seconds"] for item in cases)
    methods.append({
        "id": "model_all", "label": "Qwen3-1.7B · 全部 30 篇诊断", "version": "solid-tumor-decision-v1 / multi_axis",
        "metrics": [
            fraction("整份合格且主类一致", matched, denominator, "28 篇参考明确，另外 2 篇有歧义不计；4 份协议失败保留在分母。不是所有原始判断都错。"),
            fraction("事后裸主类一致", diagnostic_matches, diagnostic_total, "只读取原始 JSON 的 relevance；1 篇非肿瘤判断一致，但 population=female 非法导致整份失败。非新实验，未将它改为合格。"),
            fraction("结构及引句校验通过", statuses["validated_candidate"], len(cases), "26 份合格输出全为 oncology_unspecified；协议通过不代表语义正确。"),
            value("原始输出泛癌类型未定", raw_counts["oncology_unspecified"], "30 份原始 JSON 中 29 份；包括未通过附加标签/引句校验的输出。", "篇"),
            value("平均推理耗时", round(seconds / len(cases), 2), "含失败尝试；不含加载、环境准备、Astra 核读及导出。", "秒/篇"),
        ],
        "conclusion": "本次失败同时包含主类集中输出泛癌，以及附加标签校验拖累端到端结果；不能概括为模型每个字段都错或模型没有分类能力。",
    })
    current = base_record("scope-pilot-20261022", "最早 20 篇 + 实体瘤 10 篇：定位主类坍缩与协议耦合", "2026-10-22", 30)
    current.update({
        "summary": "原任务是 9 类主分类与多轴标签联合输出。整份合格且主类一致 0/28；事后裸主类一致 1/28。29/30 原始输出都选了泛癌类型未定。",
        "task": {
            "question": "根据标题和摘要等信息，文章实际研究对象属于哪种肿瘤/非肿瘤范围？",
            "input": "标题、摘要、MeSH 和出版类型；即使有全文，本次模型分类也没有读全文。",
            "outputs": ["实体瘤 solid", "血液肿瘤 hematologic", "实体/血液混合 mixed", "实体/良性或癌前边界 solid_boundary", "良性 benign", "癌前 precursor", "泛癌类型未定 oncology_unspecified", "非肿瘤 non_neoplastic", "信息不足 insufficient", "附加器官、组织学、研究类型、主题、人群标签及原文引句"],
            "not_measured": ["三类简化任务的表现（尚未运行）", "器官和癌种标签的总体准确率", "全文证据提取质量", "实体与关系抽取质量", "后训练收益（本轮未训练）"],
        },
        "dataset": {"label": "固定最早入库 20 篇 + 独立补充实体瘤 10 篇", "role": "development", "notes": [
            "前 20 篇保持入库顺序，其中 11 篇可用解析全文、9 篇仅摘要；5 篇有历史实验暴露。",
            "补充 10 篇来自已下载全文，覆盖 10 种实体瘤，无前 20 篇重复；是目的性选样，不是随机保留集。",
            "30 篇全部是开发材料，不能当作临床金标准或全库准确率。",
        ]},
        "reference": {"label": "Astra 独立参考 v1", "status": "model_reference_not_human_gold", "note": "gpt-6-astra 在未看规则/小模型输出前核读同一元数据；2 篇保留歧义。是模型参考，非人工金标准。"},
        "methods": methods,
        "findings": [
            "0/28 是严格端到端结果。PMID 42872560 原始主类 non_neoplastic 与参考一致，但非法 population=female 使整份输出未通过；事后仅主类为 1/28。",
            "26 份合格输出全选 oncology_unspecified；全部原始输出中该类为 29/30，说明主类集中到兜底类是真实问题，不能只怪格式。",
            "Qwen3-1.7B 使用非思考、FP16、greedy 解码，最大输出 768 tokens；没有领域后训练。不同提示词/解码/训练后的模型尚未在此组验证。",
            "当前协议把主分类与附加标签捆绑，单个无关标签非法会让整篇失败；下一轮应独立报告主类正确、拒答与格式失败。旧 9 分类作为已废弃任务设计保留历史。",
            "规则：最早 20 篇明确范围一致 14/16；实体瘤补充组 8/10。两组分母不同，已将歧义及旧 scope 无法区分的非肿瘤单列。",
            "Astra 复核 4 篇、9 项争议没有要求修订参考 v1；如后续发现真实新依据，应新增参考版本并统一重算全部方法。",
        ],
        "next_steps": [
            "已选定下一版只用三个业务类别：实体瘤、血液肿瘤、非肿瘤，与后续数据库对应；三分类尚未实测。",
            "无法可靠判定或处理错误记录为处理状态，category=null，不增加第四个业务类别；良性、癌前和混合研究如何映射需在标注规范中核对，不能自动归入非肿瘤。",
            "只输出一个主类和一条原文依据，主类、引句、格式分别评分；癌种与其他标签后置。",
            "先固定同一组参考和输入，对提示词/结构约束作单变量对照，再决定是否扩展模型或后训练。",
        ],
        "identity": {"task_version": "document_disease_scope/title_abstract_mesh_publication_types/v1", "sample_versions": sample_versions, "method_versions": sorted(set(method_versions)), "reference_versions": reference_versions, "completeness": "frozen"},
        "provenance": sources, "cases": cases,
    })
    summaries = {cohort: read_json(EXPERIMENTS / cohort / "summary.json") for cohort in COHORT_LABELS}
    passage = base_record("passage-candidates-20261022", "证据候选段落：已生成 368 段，尚无质量评分", "2026-10-22", 30)
    passage.update({
        "stage": "passages", "status": "unscored",
        "summary": "宽松关键词脚本召回 368 个候选段落：前 20 篇 127 段、补充 10 篇 241 段；这不是 368 条已验证证据。",
        "task": {"question": "哪些已解析段落值得进入后续证据句筛选？", "input": "可用的解析全文段落；无全文时使用已保存摘要。", "outputs": ["候选段落、原文定位与命中原因"], "not_measured": ["证据句精确率/召回率", "实体识别与标准化", "关系与适用条件", "临床证据可发布性"]},
        "dataset": current["dataset"],
        "reference": {"label": "尚无段落/证据句参考", "status": "not_configured", "note": "Astra v1 只核读文章主对象分类，不是全文证据金标准。"},
        "methods": [{
            "id": "wide_rules", "label": "宽松关键词候选召回", "version": "evidence-pilot-v1 / pilot_candidates",
            "metrics": [
                value("前 20 篇候选", summaries["evidence-pilot-20261022-first20"]["candidate_passages"], "16 篇有候选；数量不代表正确。", "段"),
                value("实体瘤补充候选", summaries["evidence-pilot-20261022-solid10"]["candidate_passages"], "10 篇均有候选；数量不代表正确。", "段"),
                value("已发布临床证据", 0, "候选仍是研究材料，未通过正式证据提取/审核。", "条"),
            ],
            "conclusion": "先证明原文定位和恢复链路可运行；没有足够参考判断哪种证据筛选方法更好。",
        }],
        "findings": [
            "候选可能包含背景、方法、图注与参考文献；这些类别不能一概视为有效结果，也不能不看上下文全删。",
            "观察到同一参考文献重复、撤稿通知参考列表命中、患者数与病灶数容易混淆等问题，需单独建立证据句参考。",
            "本轮没有运行证据句小模型、实体抽取模型或训练。",
        ],
        "next_steps": ["先给候选标注章节与句子边界，再建立少量证据句正负例和困难例；引用和适用条件保留到后续关系抽取。"],
        "identity": {"task_version": "evidence-pilot-v1/candidate_passages", "sample_versions": sample_versions, "method_versions": sorted({read_json(EXPERIMENTS / cohort / "results/case-01.json")["config_sha256"] for cohort in COHORT_LABELS}), "reference_versions": ["not_configured"], "completeness": "frozen"},
        "provenance": [provenance(EXPERIMENTS / cohort / "summary.json", f"{label} 候选数量") for cohort, label in COHORT_LABELS.items()],
    })
    return [current, passage]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "apps/curation/research_data")
    args = parser.parse_args()
    from apps.curation.research_history import register_record

    # Build and validate all source projections before registering any new record.
    records = historical_records() + current_records()
    for record in records:
        result = register_record(record, directory=args.output)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
