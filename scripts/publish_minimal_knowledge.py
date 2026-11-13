"""Publish the measured 15-group minimal-knowledge ablation, without model calls."""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.decision import digest
from apps.curation.research_history import (
    DIRECTORY,
    MAX_BYTES,
    prepare_record,
    read_record,
    register_record,
)
from scripts.benchmark_knowledge_candidates import completed_results, load_inputs
from scripts.evaluate_core_knowledge import require
from scripts.evaluate_knowledge_candidates import read, sha
from scripts.evaluate_summary_ablation import arm_timing
from scripts.publish_summary_ablation import ratio

RID = "minimal-knowledge-20261101"
ROOT = PROJECT / "var/experiments" / RID
BASELINE = PROJECT / "var/experiments/bonsai-relations-20261030"
ENTITIES = PROJECT / "var/experiments/lean-entities-20261029"
DOC = "docs/research/021-minimal-knowledge-loop.md"
VERSION = "minimal-knowledge-publication-v1"
IDS = [f"K{i:02d}" for i in range(1, 16)]
ARMS = ("A", "B")
LABELS = {"A": "A · 9B独立实体 + Bonsai关系", "B": "B · Bonsai单次实体与关系"}
DIMENSIONS = ("core_valid", "target_covered", "participants_usable")


def indexed(records, key="id"):
    result = {r[key]: r for r in records}
    require(list(result) == IDS and len(records) == 15, f"Expected ordered unique 15 {key} values")
    return result


def verify_quotes(value, passages):
    if isinstance(value, dict):
        passage_id = value.get("passage", value.get("passage_id"))
        if "quote" in value and passage_id is not None:
            require(passage_id in passages and isinstance(value["quote"], str)
                    and bool(value["quote"]) and value["quote"] in passages[passage_id]["text"],
                    "Reviewed quote is not exact original text")
            if "start" in value or "end" in value:
                require(type(value.get("start")) is int and type(value.get("end")) is int
                        and 0 <= value["start"] < value["end"] <= len(passages[passage_id]["text"])
                        and passages[passage_id]["text"][value["start"]:value["end"]] == value["quote"],
                        "Reviewed quote coordinates changed")
        for child in value.values():
            verify_quotes(child, passages)
    elif isinstance(value, list):
        for child in value:
            verify_quotes(child, passages)


def checked_bundle(row, packet, result):
    require(row["article"] == packet["article"] and row["version_id"] == packet["article"]["version_id"]
            and row["source_sha256"] == packet["article"]["source_sha256"], "Bundle article identity changed")
    require(row["provenance"]["result_sha256"] == digest(result)
            and row["provenance"]["input_sha256"] == result["input_sha256"]
            and row["provenance"]["raw_fact"] == (result.get("payload") or {}).get("fact"),
            "Bundle no longer represents reviewed raw result")
    passages = {p["id"]: p for p in packet["passages"]}
    for anchor in row["anchors"]:
        require(anchor["passage_id"] in passages, "Unknown source paragraph")
        passage = passages[anchor["passage_id"]]
        start, end = anchor["start"], anchor["end"]
        require(type(start) is int and type(end) is int and 0 <= start < end <= len(passage["text"])
                and passage["text"][start:end] == anchor["quote"]
                and anchor["parser_version"] == passage["parser_version"], "Source coordinates changed")
    if result["status"] != "classified":
        require(not row["fact"] and not row["entities"], "Failed inference cannot supply rescued knowledge")


def load_study(root, reference_path, baseline_review_path, direct_review_path, assembly_review_path):
    artifacts = {}

    def load(path, kind="method"):
        path = Path(path).absolute()
        artifacts[str(path)] = {"path": path, "sha256": sha(path), "kind": kind}
        return read(path)

    def run(folder):
        inputs, definition = load(folder / "inputs.json", "sample"), load(folder / "definition.json")
        checked_inputs = load_inputs(folder / "inputs.json")
        require(checked_inputs == inputs, "Model input loader differs")
        manifest, summary = load(folder / "extract/manifest.json"), load(folder / "extract/summary.json")
        require(manifest["records"] == 15 and manifest["input_sha256"] == digest(inputs)
                and manifest["definition_sha256"] == digest(definition)
                and manifest["config_sha256"] == digest(manifest["config"])
                and manifest["config"]["definition"] == definition, "Model input/config identity changed")
        results = completed_results(folder / "extract", inputs["records"], manifest["config_sha256"])
        require(set(results) == set(IDS), "Missing original model results")
        for rid in IDS:
            load(folder / "extract" / f"result-{rid}.json")
        require(summary["training"] is False and summary["statuses"] == dict(Counter(r["status"] for r in results.values())),
                "Run status totals changed")
        timing = arm_timing(summary, list(results.values()), manifest)
        timing["scored_sessions"] = sorted({result["session"] for result in results.values()})
        return results, timing

    reference = load(reference_path, "reference")
    initial = {"A": load(baseline_review_path, "reference"), "B": load(direct_review_path, "reference")}
    final = load(assembly_review_path, "reference")
    previous_path = root / "assembly-review-v2.json"
    previous = load(previous_path, "reference")
    require(final["previous_assembly_review_sha256"] == sha(previous_path)
            and previous["model"] == "gpt-6-astra" and previous["status"] == "complete"
            and previous["reference_sha256"] == sha(reference_path), "Previous assembly review changed")
    require(reference["model"] == final["model"] == "gpt-6-astra"
            and reference["status"] == "frozen" and final["status"] == "complete", "Need frozen reference and completed Astra review")
    require(final["reference_sha256"] == sha(reference_path), "Assembly review reference changed")
    for arm, path in zip(ARMS, (baseline_review_path, direct_review_path), strict=True):
        require(initial[arm]["model"] == "gpt-6-astra" and initial[arm]["status"] == "frozen"
                and initial[arm]["reference_sha256"] == sha(reference_path)
                and final["prior_review_sha256"][arm] == sha(path), "Original semantic/participation review changed")
    packets = indexed(load(BASELINE / "packets.json", "sample")["records"])
    clean_packets = indexed(load(root / "direct/packets.json", "sample")["records"])
    plan = load(root / "direct/plan.json")
    require(plan["source_packets_sha256"] == sha(BASELINE / "packets.json")
            and plan["source_inventory_in_model_input"] is False and plan["records"] == 15,
            "Direct arm source-only plan changed")
    for rid in IDS:
        require(clean_packets[rid] == {key: value for key, value in packets[rid].items() if key != "entities"},
                "Direct arm original source text differs")
    results_a, timing_a = run(BASELINE)
    results_b, timing_b = run(root / "direct")
    _, timing_entities = run(ENTITIES)
    results = {"A": results_a, "B": results_b}
    references, initial_rows, final_rows = indexed(reference["records"]), {arm: indexed(r["records"]) for arm, r in initial.items()}, indexed(final["records"])
    previous_rows = indexed(previous["records"])
    old_bundles, bundles = {}, {}
    for arm, name in (("A", "baseline"), ("B", "direct")):
        old_path, path = root / name / "bundle.json", Path(final["bundle_paths"][arm])
        require(path.resolve().is_relative_to(root.resolve()), "Reviewed bundle is outside this experiment")
        original = load(old_path)
        current = load(path)
        old_bundles[arm], bundles[arm] = indexed(original["records"], "group_id"), indexed(current["records"], "group_id")
        require(current["provenance"]["version"].startswith("minimal-knowledge-assembly-v"), "Expected reviewed final assembly")
        old_sha = (initial[arm]["assembly_participant_review"]["bundle_sha256"] if arm == "A"
                   else initial[arm]["bundle_sha256"])
        require(old_sha == sha(old_path) and final["bundle_sha256"][arm] == sha(path), "Reviewed bundle changed")
        previous_bundle_path = root / f"{name}-v2/bundle.json"
        load(previous_bundle_path)
        require(previous["bundle_sha256"][arm] == sha(previous_bundle_path), "Previous reviewed bundle changed")
        load(root / name / "assembly.json")
        load(path.parent / "assembly.json")
    for rid in IDS:
        packet = packets[rid]
        passages = {p["id"]: p for p in packet["passages"]}
        verify_quotes(references[rid], passages)
        require(set(final_rows[rid]["arms"]) == set(ARMS), "Missing paired assembly reviews")
        for arm in ARMS:
            raw, before, after = results[arm][rid], initial_rows[arm][rid], final_rows[rid]["arms"][arm]
            result_sha = initial[arm]["result_sha256"][rid] if arm == "A" else before["result_sha256"]
            source = (BASELINE if arm == "A" else root / "direct") / "extract" / f"result-{rid}.json"
            require(result_sha == sha(source), "Reviewed original output changed")
            if arm == "B":
                require(before["result_status"] == raw["status"], "Reviewed output status changed")
            for dimension in DIMENSIONS:
                require(type(before[dimension]) is bool and type(after[dimension]) is bool, "Missing boolean review decision")
            require(after["core_valid"] == before["core_valid"] and after["target_covered"] == before["target_covered"]
                    and after["original_participants_usable"] == before["participants_usable"]
                    and after["previous_participants_usable"] == previous_rows[rid]["arms"][arm]["participants_usable"],
                    "Assembly review changed original semantic scores or revision history")
            require(after["status"] == ("usable" if after["participants_usable"] else "needs_revision"), "Review state inconsistent")
            if raw["status"] != "classified":
                require(not any(after[key] for key in DIMENSIONS), "Incomplete model output counted as success")
            verify_quotes(before, passages)
            verify_quotes(after, passages)
            for bundle in (old_bundles[arm], bundles[arm]):
                checked_bundle(bundle[rid], packet, raw)
            derived = {"entity_roles", "entity_ids", "drug_names"}
            require({k: v for k, v in old_bundles[arm][rid]["fact"].items() if k not in derived}
                    == {k: v for k, v in bundles[arm][rid]["fact"].items() if k not in derived}, "Assembly changed raw relation semantics")
            require(after["entity_roles_observed"] == bundles[arm][rid]["fact"].get("entity_roles", {}), "Reviewed participant roles changed")
    scores = {}
    for arm in ARMS:
        rows = [final_rows[rid]["arms"][arm] for rid in IDS]
        scores[arm] = {key: sum(row[key] for row in rows) for key in (*DIMENSIONS, "original_participants_usable", "previous_participants_usable")}
        scores[arm]["all_three"] = sum(all(row[key] for key in DIMENSIONS) for row in rows)
        scores[arm]["json_complete"] = sum(results[arm][rid]["status"] == "classified" for rid in IDS)
        scores[arm]["truncated"] = sum(results[arm][rid]["status"] == "output_truncated" for rid in IDS)
        require(all(final["summary"][arm][key] == scores[arm][key] for key in (*DIMENSIONS, "original_participants_usable", "previous_participants_usable")),
                "Review summary differs from per-group scores")
    for path in sorted((root / "direct/extract").glob("server-*.log")):
        artifacts[str(path)] = {"path": path, "sha256": sha(path), "kind": "method"}
    for name in (DOC, "scripts/publish_minimal_knowledge.py", "scripts/minimal_knowledge_pilot.py",
                 "apps/curation/minimal_knowledge.py"):
        path = PROJECT / name
        artifacts[str(path)] = {"path": path, "sha256": sha(path), "kind": "method"}
    return {"packets": packets, "references": references, "initial": initial_rows, "final": final_rows,
            "bundles": bundles, "results": results, "scores": scores, "artifacts": list(artifacts.values()),
            "timing": {"A_entities": timing_entities, "A_relations": timing_a, "B": timing_b},
            "policy_clarification": final["policy_clarification"]}


def count_metric(label, count, note=""):
    return {"label": label, "value": count, "note": note}


def method(study, arm):
    score, timing = study["scores"][arm], study["timing"]
    seconds = timing["A_entities"]["inference_seconds"] + timing["A_relations"]["inference_seconds"] if arm == "A" else timing["B"]["inference_seconds"]
    metrics = [
        ratio("核心关系正确", score["core_valid"], 15, "只评实际输出的核心癌种、治疗、方向及必要条件；与是否完成指定目标独立。"),
        ratio("指定目标覆盖", score["target_covered"], 15, "覆盖ANCHOR主题；覆盖不表示核心关系正确。"),
        ratio("最终组装参与实体及角色可用", score["participants_usable"], 15, "审核实际组装身份、类型及角色；不能用实体库存存在替代关系参与。"),
        ratio("三项同时通过", score["all_three"], 15, "以上三项交集；不等于标准ID确认或生产临床验证。"),
        ratio("初版参与层可用（保留）", score["original_participants_usable"], 15,
              "最终分数包含组装修复及明确的非目标诊断口径澄清，不把全部变化归因为模型或工程改进。"),
        ratio("完整JSON输出", score["json_complete"], 15, "B截断保持失败，不补全或二次模型修复。"),
        count_metric("输出截断", score["truncated"], "按原始结果status计数。"),
        {"label": "累计推理耗时", "value": round(seconds, 2), "unit": "秒",
         "note": "010实体与019关系历史分段耗时相加，未实测串联端到端耗时。" if arm == "A" else
                 f"15条来自{len(timing['B']['scored_sessions'])}个实际推理会话；不含加载、预热、脚本及审核。保留原summary的非完整整批计时标记，不据此声称端到端提速。"},
    ]
    if arm == "A":
        metrics += [{"label": label, "value": timing[key]["peak_process_mib"], "unit": "MiB", "note": "历史单进程NVML峰值，非串联/同时驻留总显存。"}
                    for label, key in (("9B实体进程显存", "A_entities"), ("Bonsai关系进程显存", "A_relations"))]
    else:
        metrics += [{"label": "进程显存峰值", "value": timing["B"]["peak_process_mib"], "unit": "MiB", "note": "本轮NVML进程峰值。"}]
    return {"id": arm, "label": LABELS[arm], "version": VERSION, "metrics": metrics,
            "conclusion": "效果优先，保留独立9B实体与Bonsai关系方案。" if arm == "A" else "本轮删除独立实体步骤损失质量，停止对直接方案继续调参。"}


def compact_review(row):
    keys = (*DIMENSIONS, "original_participants_usable", "previous_participants_usable", "core_errors", "missing_core",
            "participant_errors", "missing_participants", "diagnostics", "resolved_errors", "v3_resolved_errors",
            "review_basis", "policy_reclassification_only")
    def without_quotes(node):
        if isinstance(node, dict):
            return {k: without_quotes(v) for k, v in node.items() if k != "source_quotes"}
        if isinstance(node, list):
            return [without_quotes(v) for v in node]
        return node
    return without_quotes({key: row[key] for key in keys if key in row})


def card(study, rid):
    packet, reviewed = study["packets"][rid], study["final"][rid]["arms"]
    anchor = [p for p in packet["passages"] if p["role"] == "anchor"]
    require(len(anchor) == 1, "Need one source ANCHOR")
    details = {}
    notes = []
    for arm in ARMS:
        bundle, review = study["bundles"][arm][rid], reviewed[arm]
        details[LABELS[arm]] = {"原始输出状态": study["results"][arm][rid]["status"],
            "原始事实": bundle["provenance"]["raw_fact"], "最终组装关系": bundle["fact"],
            "参与实体": [entity for entity in bundle["entities"] if entity["ref"] in bundle["fact"].get("entity_ids", [])],
            "审核": compact_review(review)}
        notes.append(f"{arm}：核心{'对' if review['core_valid'] else '错/缺失'}，目标{'覆盖' if review['target_covered'] else '未覆盖'}，参与层{'可用' if review['participants_usable'] else '需修订'}。")
    selected = study["bundles"]["A"][rid]
    return {"id": rid, "pmid": packet["article"]["pmid"], "title": packet["article"]["title"],
            "statement": anchor[0]["text"], "status": "supported" if all(reviewed["A"][key] for key in DIMENSIONS) else "needs_revision",
            "review_note": " ".join(notes), "study_scope": study["references"][rid]["anchor_target"],
            "source_role": "主文是原始ANCHOR，展开查看两臂实际关系和错误；整段展示不代表每句话均支持全部字段。",
            "entities": [{"id": entity["ref"], "type": entity["kind"], "text": entity["label"],
                          "normalization_status": "A方案最终组装原文参与实体；标准ID未评估", "standard_ids": []}
                         for entity in selected["entities"] if entity["ref"] in selected["fact"].get("entity_ids", [])],
            "quotes": [{"passage_id": p["id"], "locator": f"{p['alias']} / {p['role']} / {p['locator']}",
                        "quote": p["text"], "fields": ["冻结原文上下文"], "start": 0, "end": len(p["text"]),
                        "version_id": p["version_id"], "source_sha256": p["source_sha256"], "parser_version": p["parser_version"]}
                       for p in packet["passages"]], "article_provenance": packet["article"],
            "mapping": {"status": "research_comparison_only", "blockers": ["本页未发布生产知识；标准ID和临床适用性未评估。"],
                        "preview": {"目标参考": study["references"][rid], **details}}}


def build(root=ROOT, *, reference=None, baseline_review=None, direct_review=None, assembly_review=None):
    root = Path(root).absolute()
    study = load_study(root, reference or root / "reference.json", baseline_review or root / "baseline-review.json",
                       direct_review or root / "direct-review.json", assembly_review or root / "assembly-review-v3.json")
    scores = study["scores"]
    summary = (f"同5篇15组，保留9B实体+Bonsai关系。核心正确A {scores['A']['core_valid']}/15、B {scores['B']['core_valid']}/15；"
               f"目标覆盖A {scores['A']['target_covered']}/15、B {scores['B']['target_covered']}/15；最终参与层A {scores['A']['participants_usable']}/15、B {scores['B']['participants_usable']}/15。")
    artifacts = study["artifacts"]
    def artifact_name(path):
        try:
            return str(path.relative_to(PROJECT))
        except ValueError:
            return str(path)
    date = read_record(RID)["date"] if (DIRECTORY / f"{RID}.json").exists() else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    record = prepare_record({
        "schema_version": "research-history-v1", "id": RID, "title": "021 · 最小知识闭环：保留独立实体步骤",
        "date": date, "stage": "knowledge", "status": "evaluated", "task_kind": "knowledge_candidates",
        "sample_count": 15, "sample_unit": "片段组", "summary": summary,
        "task": {"question": "能否删除独立实体模型，用一次Bonsai抽取同时得到可追溯、可用的实体与关系？",
                 "input": "同5篇15个冻结ANCHOR/SUPPORT组；A复用010实体与019关系，B只读原文并一次抽取。",
                 "outputs": ["核心正确、目标覆盖、参与实体与角色三项独立评分", "同组原文和主要错误", "原输出与最终组装结果分开保留"],
                 "not_measured": ["新文献独立测试和全库效果", "全文到知识的端到端速度", "标准ID准确率及生产临床适用性"]},
        "dataset": {"label": "同5篇15组开发回归消融", "document_count": 5, "role": "冻结开发集",
                    "notes": ["15组均计入分母；截断不是弃样。未新增模型训练或直接方案调参。",
                              "本记录只归档15组实际消融；更多文献段落的工程流程验证另行记录。",
                              "组装修订和非目标诊断口径澄清分别记录，不将全部分数变化归为模型提升。"]},
        "reference": {"label": "冻结最小核心目标 + Astra逐条原文审核", "status": "model_reference_not_human_gold",
                      "note": "采用新的最小目标参考重新审核A/B，不沿用019通过率。组装复核继承初审核心与覆盖，另审参与层；AI参考不是人工金标准。"},
        "methods": [method(study, arm) for arm in ARMS],
        "findings": [
            "删除独立实体步骤后，直接方案仍能写出部分正确关系，但组合药成分、原词定位、细胞模型类型和参与角色更易丢失。质量优先保留A，不继续调B。",
            f"最终三项同时通过：A {scores['A']['all_three']}/15、B {scores['B']['all_three']}/15；核心正确和目标覆盖不可互相替代。",
            f"初版参与层A {scores['A']['original_participants_usable']}/15、B {scores['B']['original_participants_usable']}/15分别保留。类别保护、明确原文缩写、否定/既往用药角色属于组装修复；非目标分子类型转诊断属于口径澄清，问题仍保留。",
            "A的K04输出了正确额外事实但没有覆盖安全性目标；B的K04、K14截断，K07、K13有队列/终点范围问题。逐组页保留原文与审核原因。",
            study["policy_clarification"],
            "A时间为历史实体和关系生成之和；B首次加载失败未产生结果，15条均来自第二个会话。保留原计时标记；两者均未测完整流水线延迟，不报告端到端加速比。",
        ],
        "next_steps": ["保留9B实体+Bonsai关系，先处理仍阻塞必要身份/角色绑定的组装问题。", "更多全文段落的工程验证单独归档，通过与本页相同的出处和状态校验后再评估生产使用。"],
        "identity": {"task_version": VERSION, "completeness": "frozen",
                     **{field: [item["sha256"] for item in artifacts if item["kind"] == kind]
                        for field, kind in (("sample_versions", "sample"), ("method_versions", "method"), ("reference_versions", "reference"))}},
        "provenance": [{"label": artifact_name(item["path"]), "artifact": artifact_name(item["path"]), "sha256": item["sha256"]} for item in artifacts],
        "knowledge_candidates": [card(study, rid) for rid in IDS],
    })
    require(len(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode()) + 1 <= MAX_BYTES, "Research record too large")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("reference", "baseline-review", "direct-review", "assembly-review"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--preview", action="store_true", help="Validate actual sources and show a compact summary without registration")
    args = parser.parse_args()
    record = build(args.root, reference=args.reference, baseline_review=args.baseline_review,
                   direct_review=args.direct_review, assembly_review=args.assembly_review)
    print(json.dumps({"id": record["id"], "summary": record["summary"], "methods": record["methods"],
                      "cases": len(record["knowledge_candidates"]), "provenance": len(record["provenance"]),
                      "bytes": len(json.dumps(record, ensure_ascii=False).encode())} if args.preview else register_record(record),
                     ensure_ascii=False, indent=2))
