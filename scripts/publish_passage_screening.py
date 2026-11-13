#!/usr/bin/env python3
"""Project a completed, offline passage evaluation into research record 005.

Writes a public JSON draft and a concise research document. --publish additionally
registers the immutable website record. It never accesses a database or runs models.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.pilot_candidates import VERSION as RULE_VERSION, _CUE_PATTERNS
from apps.curation.research_history import prepare_record, register_record

BASE = ROOT / "var/experiments/evidence-passages-20261023"
RECORD_ID = "treatment-passages-five-papers-20261023"
METHODS = {
    "rules": ("原规则", "同一段至少命中治疗、结局、生物标志物三类词中的两类。否定词不用于排除。"),
    "qwen35-4b": ("Qwen3.5-4B", "逐段判断中心段落；标题和前后段仅供上下文。"),
    "qwen35-9b": ("Qwen3.5-9B", "相同输入、提示词和FP16协议，作为容量对照。"),
    "rules-and-4b": ("规则 AND 4B", "规则先筛，命中的段落再由4B判断；未命中直接不选。组合速度为离线估算。"),
    "rules-or-4b": ("规则 OR 4B", "规则命中即保留，未命中再由4B判断以补漏。组合速度为离线估算。"),
}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def percentage(value):
    return "未定义" if value is None else f"{100 * value:.1f}%"


def safe_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def ratio_metric(label, numerator, denominator, note):
    if denominator:
        return {"label": label, "numerator": numerator, "denominator": denominator, "note": note}
    return {"label": label, "value": "未定义", "note": note + "；分母为0。"}


def method_card(name, result, config_sha):
    label, conclusion = METHODS[name]
    timing = result["timing"]
    estimated = name in ("rules-and-4b", "rules-or-4b")
    timing_label = "离线估算" if estimated else "实测"
    timing_note = ("复用同一次4B逐段耗时，按所需调用求和加规则扫描；未单独运行组合流水线。"
                   if estimated else "包含逐段分词、传输、推理和输出校验；不含模型加载、预热和写文件。"
                   if name.startswith("qwen") else "只计CPU规则扫描，不使用模型。")
    metrics = [
        ratio_metric("精确率 Precision", result["tp"], result["tp"] + result["fp"], "分母为参考明确且方案选中的段落。"),
        ratio_metric("召回率 Recall", result["tp"], result["tp"] + result["fn"], "分母为Astra参考阳性；模型未决或失败计入漏检。"),
        {"label": "F1", "value": percentage(result["f1"]), "note": "精确率与召回率的调和平均。"},
        {"label": "命中 / 漏检 / 误收（TP / FN / FP）", "value": f"{result['tp']} / {result['fn']} / {result['fp']}", "unit": "段", "note": "只统计参考明确的段落。"},
        {"label": "全部入选 selected_all", "value": result["selected_all"], "unit": "段", "note": f"以全部{result['total']}段为范围，包含参考未决段落。"},
        {"label": "待判定 pending", "value": result["pending_all"], "unit": "段", "note": "模型null、非法输出、超长或截断不能当作确定阴性。"},
        {"label": f"{timing_label}总处理时间", "value": round(timing["seconds"], 3), "unit": "秒", "note": timing_note},
        {"label": f"{timing_label}平均每段", "value": round(timing["seconds"] / result["total"], 4), "unit": "秒/段", "note": f"总处理时间÷全部{result['total']}段，不只除以模型实际调用数。"},
        {"label": "模型调用", "value": timing["model_calls"], "unit": "次", "note": "规则方案为0；组合为离线推导所需次数。"},
    ]
    summary = timing.get("summary", {})
    if "load_seconds" in summary:
        metrics.append({"label": "模型加载（单列）", "value": round(summary["load_seconds"], 3), "unit": "秒",
                        "note": "冷启动模型实例；操作系统文件缓存状态未隔离，不包含在逐段处理时间中。"})
    if "nvml_process_peak_mib" in summary:
        metrics.append({"label": "峰值显存", "value": round(summary["nvml_process_peak_mib"] / 1024, 2), "unit": "GiB",
                        "note": "NVML进程显存峰值；0.1秒采样。"})
    if summary.get("timing_resumed"):
        conclusion += " 本次从断点续跑，计时非单次连续完整运行。"
    return {"id": name, "label": label, "version": config_sha, "metrics": metrics, "conclusion": conclusion}


def example_findings(notes, cases):
    examples = notes.get("examples", [])
    if not isinstance(examples, list) or len(examples) > 2:
        raise ValueError("At most two public FP/FN examples are permitted")
    findings = []
    by_id = {case["id"]: case for case in cases}
    for item in examples:
        case = by_id[item["id"]]
        method, error, reason = item["method"], item["error"], item["reason"]
        if method not in METHODS or not isinstance(reason, str) or not 1 <= len(reason) <= 240:
            raise ValueError("Invalid example method or short reason")
        actual = case["reference"]["relevant"]
        predicted = case["predictions"][method]["relevant"]
        if not ((error == "FN" and actual is True and predicted is not True)
                or (error == "FP" and actual is False and predicted is True)):
            raise ValueError("Example is not the declared scored FP/FN")
        findings.append(f"{METHODS[method][0]} {error}｜PMID {case['pmid']}｜{case['locator']}：{reason}")
    return findings


def local_artifact(base, filename):
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError("Reference artifacts must be filenames within the experiment directory")
    path = base / filename
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Reference artifact resolves outside the experiment directory")
    return path


def build(base, notes=None, evaluation_path=None):
    notes = notes or {}
    for key in ("findings", "next_steps"):
        if not isinstance(notes.get(key, []), list) or any(not isinstance(value, str) or not value for value in notes.get(key, [])):
            raise ValueError(f"notes.{key} must be a list of nonempty strings")
    evaluation_path = Path(evaluation_path).resolve() if evaluation_path is not None else base / "evaluation.json"
    evaluation = read(evaluation_path)
    reference_path = local_artifact(base, evaluation.get("reference_artifact", "astra-reference-v1.json"))
    reference = read(reference_path)
    parent_reference_sha256 = evaluation.get("parent_reference_sha256", reference.get("parent_reference_sha256"))
    review_artifact = evaluation.get("review_artifact", reference.get("review_artifact"))
    revised_reference = bool(parent_reference_sha256)
    extra_reference_paths = []
    reference_versions = [evaluation["reference_sha256"]]
    if revised_reference:
        parent_reference_path = local_artifact(base, "astra-reference-v1.json")
        if sha(parent_reference_path) != parent_reference_sha256:
            raise ValueError("Parent reference checksum differs from the retained v1")
        initial_evaluation_path = base / "evaluation.json"
        if read(initial_evaluation_path)["reference_sha256"] != parent_reference_sha256:
            raise ValueError("Initial evaluation does not refer to the retained v1 reference")
        review_path = local_artifact(base, review_artifact or "reference-review.json")
        extra_reference_paths = [("保留的初始Astra参考v1", parent_reference_path),
                                 ("保留的初评v1", initial_evaluation_path),
                                 ("候选分歧复核", review_path)]
        reference_versions += [parent_reference_sha256, sha(review_path)]
    selection = read(base / "selection.json")
    definition = read(base / "definition.json")
    inputs = read(base / "inputs.json")
    research = read(base / "model-research.json")
    rows = inputs["records"]
    selected = selection["selected_documents"]
    if len(rows) != 282 or len(selected) != 5 or set(evaluation["methods"]) != set(METHODS):
        raise ValueError("Expected five papers, 282 paragraphs and the five frozen methods")
    if evaluation["input_sha256"] != digest(rows) or selection["input_records_sha256"] != digest(rows):
        raise ValueError("Evaluation/selection input hashes differ")
    if evaluation["definition_sha256"] != sha(base / "definition.json") or evaluation["reference_sha256"] != sha(reference_path):
        raise ValueError("Definition or reference has changed since evaluation")
    if evaluation["rule_source_sha256"] != sha(ROOT / "apps/curation/pilot_candidates.py"):
        raise ValueError("Rule source has changed since evaluation")
    documents = {item["pmid"]: read(base / item["provenance_file"]) for item in selected}
    if set(documents) != {row["pmid"] for row in rows}:
        raise ValueError("Selected papers and input papers differ")
    totals = {key: sum(document["coverage"][key] for document in documents.values()) for key in
              ("parsed_fulltext_passages", "jats_table_count", "jats_figure_count", "paragraphs_nested_in_paragraphs")}
    if totals["parsed_fulltext_passages"] != len(rows):
        raise ValueError("Parsed source coverage differs from evaluated inputs")
    manifests = {name: read(base / name / "manifest.json") for name in ("qwen35-4b", "qwen35-9b")}
    for manifest in manifests.values():
        if manifest["config_sha256"] != digest(manifest["config"]) or manifest["input_sha256"] != digest(inputs):
            raise ValueError("Run manifest integrity mismatch")
        if manifest["config"]["definition"] != definition:
            raise ValueError("Run definition differs")
    method_versions = {name: manifests[name]["config_sha256"] if name in manifests else
                       digest({"method": name, "rules": evaluation["rule_source_sha256"],
                               "4b": manifests["qwen35-4b"]["config_sha256"] if name != "rules" else None})
                       for name in METHODS}
    first = evaluation["methods"]["rules"]
    denominator = first["denominator"]
    for result in evaluation["methods"].values():
        if result["denominator"] != denominator or result["total"] != len(rows):
            raise ValueError("Method denominators differ")
    coverage_note = (f"仅评估已有解析器的{len(rows)}个全文p段落；源文共{totals['jats_table_count']}表、"
                     f"{totals['jats_figure_count']}图，尚未完整恢复表格关系和图像内容。"
                     f"{totals['paragraphs_nested_in_paragraphs']}个嵌套段落可能与上级文本重叠，不能当作独立事实数量。")
    findings = [
        f"5篇论文的{len(rows)}段全部参与评测；Astra明确参考{denominator}段，其中阳性{first['positive']}、阴性{first['negative']}；参考未决{first['unscored']}段排除于主指标。",
        "原规则未针对本轮错误改词：同段治疗、结局、生物标志物三类提示词至少出现两类即可入选；否定及比较词只保留标记，不决定筛选。",
        "AND和OR均复用同一完整4B输出；组合耗时按实际逐段延迟离线估算，并非重新实测整条流水线。",
        coverage_note,
        "Astra为模型参考，非人工金标准。目的性选择的5篇开发样本，不能据此推断全库性能或临床适用性。",
        *notes.get("findings", []), *example_findings(notes, evaluation["cases"]),
    ]
    provenance_paths = [
        ("冻结输入", base / "inputs.json"), ("选样与范围", base / "selection.json"),
        ("任务定义", base / "definition.json"), ("当前Astra参考", reference_path),
        ("全量离线评测", evaluation_path), ("模型资料与筛选", base / "model-research.json"),
        *extra_reference_paths,
        ("原规则", ROOT / "apps/curation/pilot_candidates.py"),
        ("推理脚本", ROOT / "scripts/benchmark_passage_screening.py"),
        ("评分脚本", ROOT / "scripts/evaluate_passage_screening.py"),
        ("发布投影脚本", Path(__file__).resolve()),
        *[(f"PMID {pmid} 原文版本与覆盖", base / f"documents/{pmid}.json") for pmid in documents],
        *[(f"{METHODS[name][0]} {kind}", base / name / f"{kind}.json")
          for name in manifests for kind in ("manifest", "summary")],
    ]
    provenance = [{"label": label, "artifact": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                   "sha256": sha(path)} for label, path in provenance_paths]
    record = prepare_record({
        "schema_version": "research-history-v1", "id": RECORD_ID, "date": "2026-10-23",
        "title": "005 · 实体瘤治疗证据段落：5篇全文对照", "stage": "passages", "task_kind": "passage_screening",
        "status": "evaluated", "sample_count": len(rows), "sample_unit": "段",
        "summary": notes.get("summary", "5篇已有全文、282段，比较4B、9B、原规则及AND/OR组合的漏检、误收与速度；无训练。"),
        "task": {"question": "实体瘤治疗文献中，哪些中心段落包含可继续抽取的治疗证据？",
                 "input": "5篇全文的全部282个已解析段落；模型接收标题、位置、中心段落和相邻两段。",
                 "outputs": ["相关", "不相关", "待判定（处理状态）"],
                 "not_measured": ["表格和图像全部证据覆盖", "实体与关系抽取", "临床证据强度或适用性", "全库泛化性能"]},
        "dataset": {"label": "5篇实体瘤治疗论文 · 282段", "document_count": 5, "role": "development",
                    "notes": [selection["selection_basis"], coverage_note, "全文JATS摘要参与；PubMed元数据摘要不重复计分。"]},
        "reference": {"label": "Astra逐段参考（经分歧复核修订）" if revised_reference else "Astra初始盲标参考",
                      "status": "model_reference_not_human_gold",
                      "note": "初始Astra盲标在模型推理前冻结。"
                              + ("本次候选分歧复核修订后保留旧参考与初评，全部方案统一重算；最终修订参考不声称完全盲法。"
                                 if revised_reference else "候选分歧复核如需修订，须保留旧版并对全部方案统一重算。")
                              + f"{denominator}段明确参考用于主指标。正例引文按原文字符位置验证，未决单列。"},
        "methods": [method_card(name, evaluation["methods"][name], method_versions[name]) for name in METHODS],
        "findings": findings, "next_steps": notes.get("next_steps", ["根据本轮漏检、误收和资源成本选择下一阶段方案；不直接将段落筛选结果发布为知识。"]),
        "identity": {"task_version": definition["version"], "completeness": "frozen",
                     "sample_versions": [digest(rows), sha(base / "selection.json")],
                     "method_versions": list(method_versions.values()) + [sha(ROOT / "scripts/evaluate_passage_screening.py")],
                     "reference_versions": reference_versions},
        "provenance": provenance,
    })
    document = render_document(record, evaluation, selected, documents, research, definition, coverage_note, base)
    return record, document


def render_document(record, evaluation, selected, documents, research, definition, coverage_note, base):
    lines = ["# 005 · 实体瘤治疗证据段落筛选", "", "日期：2026-10-23。开发试验；无微调、无新增模型下载，不写回生产知识库。", "", record["summary"], "",
             "## 冻结任务与样本", "", "判断中心段落是否包含可继续抽取的治疗证据；标题、位置和前后段仅用于消除歧义，不能把邻段结论搬到中心段。", "",
             "正例包括实际治疗效果、无效结果、耐药与不良反应，明确的治疗适用条件或标准治疗表述，以及标志物与治疗反应的观察关联。临床、动物、细胞实验均可保留，但不合并证据层级；引述研究的具体结果作为二手证据保留。", "",
             "负例包括泛泛背景、研究目的与假说、仅有方案/剂量/资格/统计方法、无治疗联系的基线资料及机制、作者/伦理/经费信息。无法解释的表图片段或缺少必要上下文记待判定，不能猜测。", "",
             f"定义版本：`{definition['version']}`。完整英文提示词及输入范围见`{base.relative_to(ROOT)}/definition.json`。", "",
             "| PMID / 原文 | 标题 | 实体瘤与研究类型 | 段数 |", "|---|---|---|---:|"]
    for item in selected:
        pmid = item["pmid"]
        pmcid = documents[pmid]["article_identifiers"].get("pmcid")
        source = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        lines.append(f"| [{pmid}]({source}) | {safe_cell(item['title'])} | {safe_cell(item['tumor'])}；{safe_cell(item['study_type'])} | {item['passage_count']} |")
    lines += ["", "按治疗相关性与研究类型多样性目的性选样，未使用规则命中数或模型输出选样。所有已解析段落参与，包括未被规则命中的段落。", "", coverage_note,
              "补充文件未打开；全文自身JATS摘要参与，PubMed元数据摘要不重复计分。段落数量不是独立证据数量，也不代表全篇全部信息已经覆盖。", "",
              "## 比较方法", "", f"原规则版本：`{RULE_VERSION}`。同一段落在以下三类词中至少命中两类：", "",
              "| 类别 | 匹配内容示例 |", "|---|---|",
              "| treatment | treatment/treated、therapy、drug、chemotherapy、immunotherapy、radiotherapy、radiation、regimen、inhibitor、placebo、adjuvant/neoadjuvant |",
              "| outcome | survival、response、progression、recurrence、resistance、benefit、toxicity、adverse events、hazard ratio、PFS/OS/ORR/DFS/DOR |",
              "| biomarker | biomarker、mutation、fusion、expression、EGFR/HER2/KRAS/BRAF/ALK、PD-L1、MSI-H、dMMR、TMB |", "",
              "使用不区分大小写的英文单词边界正则；不需要三类全命中，也不要求两类位于同一句。否定/不确定及比较词只记录提示，不排除负面结果。没有自动判断研究设计、结果真实性或因果关系。", "",
              "<details><summary>本轮原始正则（可复用，不是新增词表）</summary>", "", "```json", json.dumps(_CUE_PATTERNS, ensure_ascii=False, indent=2), "```", "", "</details>", ""]
    for name, (label, description) in METHODS.items():
        lines.append(f"- **{label}**：{description}")
    lines += ["", "4B与9B使用同一冻结定义、段落顺序、FP16、单GPU、batch=1和关闭thinking；每次先预热一次再逐段计时。最大输入6144 token，禁止截断；输出上限64 token。null、非法输出、截断或超长均为待判定。", "",
              "## 其他模型资料与选择", "", "增加9B是已有权重、同系列的容量对照，可避免额外下载。官方通用基准不能保证本次证据筛选一定更好；模型选择结论以本轮结果为限。", "",
              "Qwen3-Reranker、MedCPT与BGE reranker面向查询—文档相关性排序；需要另行定义查询并用独立数据校准阈值，不能直接把检索相关性当作治疗证据。当前没有已验证的任务优势，故本轮不扩展测试。GLiNER既往项目三分类较弱，但这不证明它必然不适合本任务。", ""]
    for source in research["sources"]:
        reason = source.get("reason", "")
        lines.append(f"- [{source['model']}]({source['url']})：{source['decision']}。{reason}")
    lines += ["", "## 计分口径与结果", "", record["reference"]["note"], "", record["findings"][0], "",
              "精确率=TP/(TP+FP)，召回率=TP/(TP+FN)，F1=2TP/(2TP+FP+FN)。参考null不进入主指标；模型null或失败在参考正例上计漏检，在参考负例上不冒充确定阴性。selected_all与pending_all覆盖全部282段。", "",
              "| 方法 | Precision | Recall | F1 | TP / FN / FP | 入选 / 待定（全部） | 处理秒数 | 平均秒/段 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name in METHODS:
        result = evaluation["methods"][name]
        seconds = result["timing"]["seconds"]
        estimate = "（估算）" if name.startswith("rules-") else ""
        lines.append(f"| {METHODS[name][0]} | {percentage(result['precision'])} | {percentage(result['recall'])} | {percentage(result['f1'])} | {result['tp']} / {result['fn']} / {result['fp']} | {result['selected_all']} / {result['pending_all']} | {seconds:.3f}{estimate} | {seconds/result['total']:.4f}{estimate} |")
    lines += ["", "模型时间为逐段分词、传输、推理、解码与输出校验的累计时间，不含加载、预热、落盘。组合按规则扫描加被调用段落的既有4B延迟求和，未另行实测流水线；不能将估算写成部署吞吐。", "",
              "| 模型 | 模型加载秒数（单列） | 峰值显存 GiB | 续跑计时 |", "|---|---:|---:|---|"]
    for name in ("qwen35-4b", "qwen35-9b"):
        summary = evaluation["methods"][name]["timing"].get("summary", {})
        load = f"{summary['load_seconds']:.3f}" if "load_seconds" in summary else "未测量"
        memory = f"{summary['nvml_process_peak_mib']/1024:.2f}" if "nvml_process_peak_mib" in summary else "未测量"
        lines.append(f"| {METHODS[name][0]} | {load} | {memory} | {'是；非连续完整计时' if summary.get('timing_resumed') else '否'} |")
    lines += ["", "加载为冷启动模型实例；操作系统文件缓存未隔离，不能据此比较无缓存冷读取性能。显存为NVML进程峰值，0.1秒采样。", "",
              "## 按论文结果", "", "| PMID | 方法 | TP / FN / FP | Precision | Recall | 入选 / 待定 |", "|---|---|---:|---:|---:|---:|"]
    for item in selected:
        for name in METHODS:
            result = evaluation["methods"][name]["by_paper"][item["pmid"]]
            lines.append(f"| {item['pmid']} | {METHODS[name][0]} | {result['tp']} / {result['fn']} / {result['fp']} | {percentage(result['precision'])} | {percentage(result['recall'])} | {result['selected_all']} / {result['pending_all']} |")
    lines += ["", "## 发现与决定", "", *[f"- {finding}" for finding in record["findings"][1:]], "", "## 下一步", "", *[f"- {step}" for step in record["next_steps"]], "",
              "## 来源与复用", "", f"公开记录ID：`{record['id']}`；身份指纹：`{record['fingerprint']}`。公开页面不包含整段原文。", "",
              "完整原始引用、每段输出、计时与中间文件留在本机实验目录。本文只是冻结结果投影，未执行数据库写入、全库分类或正式知识发布。", "",
              "来源文件如下；完整SHA-256保留在公开JSON的provenance中。", ""]
    lines += [f"- {item['label']}：`{item['artifact']}`" for item in record["provenance"]]
    return "\n".join(lines) + "\n"


def ensure_writable_result(path, text):
    if path.exists() and path.read_text() != text:
        raise ValueError(f"Refusing to overwrite a different existing result: {path}")


def write_new_or_reuse(path, text):
    ensure_writable_result(path, text)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as stream:
            stream.write(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--evaluation", type=Path, help="Evaluation JSON to publish; default: BASE/evaluation.json")
    parser.add_argument("--notes", type=Path, help="Optional JSON: summary, findings, next_steps and at most two examples")
    parser.add_argument("--draft", type=Path, help="Default: BASE/public-record.json")
    parser.add_argument("--document", type=Path, default=ROOT / "docs/research/005-treatment-passages.md")
    parser.add_argument("--publish", action="store_true", help="Append immutable website record using register_record")
    args = parser.parse_args()
    record, document = build(args.base.resolve(), read(args.notes) if args.notes else None, args.evaluation)
    draft = args.draft or args.base / "public-record.json"
    payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ensure_writable_result(draft, payload)
    ensure_writable_result(args.document, document)
    status = register_record(record) if args.publish else {"status": "draft", "id": record["id"], "fingerprint": record["fingerprint"]}
    write_new_or_reuse(draft, payload)
    write_new_or_reuse(args.document, document)
    print(json.dumps({**status, "draft": str(draft), "document": str(args.document)}, ensure_ascii=False))
