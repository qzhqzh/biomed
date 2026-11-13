"""Register a read-only target compatibility preview from actual export artifacts.

No database access, model calls or target writes. Existing independent quality is
bound to its original delivery receipt, never inferred from mapping success.
"""

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.knowledge_export import digest
from apps.curation.research_history import prepare_record, register_record

RID = "target-compatibility-20261101"
ROOT = PROJECT / "var/experiments" / RID
VERSION = "target-compatibility-preview-v1"
SELECTED = PROJECT / "var/experiments/minimal-knowledge-20261101/delivery"
E2E = PROJECT / "var/experiments/minimal-e2e-20261101/delivery"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def quality_from_receipt(bundle_path, receipt, quality):
    require(receipt.get("applied") is True, "Quality receipt must represent real delivery")
    require(receipt["bundle_sha256"] == file_sha(bundle_path), "Delivered bundle SHA differs")
    # Delivery uses knowledge_storage's compact canonical JSON, also used by export.
    require(receipt["quality_input_sha256"] == digest(quality), "Independent quality SHA differs")
    require(receipt["quality"]["review_run"] == quality["review_run"], "Quality run differs")
    ids = {row["id"]: row["group_id"] for row in receipt["intake"]["candidates"]}
    require(set(ids) == {row["candidate_id"] for row in quality["records"]}, "Quality groups differ from receipt")
    return {ids[row["candidate_id"]]: {
        "status": {"pass": "valid", "fail": "invalid", "undetermined": "undetermined"}[row["decision"]],
        "reason": row["reason"], "quality_dimensions": row["quality_dimensions"],
        "review_run": quality["review_run"], "model": quality["model"],
        "note": "已有独立语义核验；本轮目标映射未重新审查或修改该结果。",
    } for row in quality["records"]}


def build_batch(bundle, exported, receipt, notes, dictionary, quality=None):
    require(exported["source_run"] == bundle["source_run"], "Export source run differs")
    require(exported["source_bundle_sha256"] == digest(bundle), "Export bundle SHA differs")
    require(exported.get("target_dictionary", {}).get("sha256") == digest(dictionary),
            "Export target dictionary snapshot differs")
    require(exported.get("target_written") is False and exported.get("submit_ready") is False,
            "Preview requires an export that has not written to the target")
    applied = receipt["intake"] if "intake" in receipt else receipt["applied"]
    require(applied["source_run"] == bundle["source_run"] and applied["dry_run"] is False,
            "Stored candidate link requires matching real import receipt")
    require(applied["import_sha256"] == digest(bundle), "Stored candidate bundle differs")
    candidates = {row["group_id"]: row["id"] for row in applied["candidates"]}
    exports = {row["group_id"]: row for row in exported["records"]}
    require(set(candidates) == set(exports) == {row["group_id"] for row in bundle["records"]},
            "Bundle, export and intake groups differ")
    targets = {row["id"]: row for kind in ("drugs", "tumors") for row in dictionary[kind]}
    result = []
    for original in bundle["records"]:
        group = original["group_id"]
        export = exports[group]
        note = notes[(bundle["source_run"], group)]
        require(export["biomed_companion"] == original, "Export companion differs from original knowledge")
        mapped = []
        for item in note["entity_mappings"]:
            item = copy.deepcopy(item)
            target_id = item.get("target_id")
            if target_id:
                require(target_id in targets, "Preview target ID is absent from dictionary")
                target = targets[target_id]
                item["target_name"] = target.get("name_en") or target["name"]
                item["target_name_cn"] = target.get("name_cn", "")
            adopted = export["resolved_entities"].get(item["entity_ref"], {}).get("target_id")
            require(not adopted or adopted == target_id, "Mapping notes disagree with actual adopted ID")
            item["accepted_by_exporter"] = bool(adopted)
            for candidate in item.get("candidates", []):
                require((candidate.get("id") or candidate.get("target_id")) in targets,
                        "Ambiguity candidate ID absent from dictionary")
            mapped.append(item)
        adaptation = export.get("adaptation", {})
        article = original["article"]
        result.append({
            "id": candidates[group], "candidate_id": candidates[group], "group_id": group,
            "source_run": bundle["source_run"],
            "article": {key: article.get(key, "") for key in ("title", "pmid", "doi")},
            "fact": copy.deepcopy(original["fact"]),
            "quality": (quality or {}).get(group, copy.deepcopy(original.get("assessment", {}).get("quality", {"status": "unreviewed"}))),
            "compatibility_status": export["compatibility_status"], "offline_ready": export["offline_ready"],
            "entity_mappings": mapped,
            "trigger_scope": adaptation.get("trigger_scope", note["trigger_scope"]),
            "treatment_scope": adaptation.get("treatment_scope", note["treatment_scope"]),
            "target_payload": export["target_payload"], "mapping_issues": export["mapping_issues"],
            "contract_validation": export["contract_validation"], "matching_validation": export.get("matching_validation", {}),
            "live_sample_matching": export.get("live_sample_matching", "unverified"),
            "review_points": note["review_points"],
            "quotes": [{key: anchor[key] for key in ("passage_id", "locator", "start", "end", "quote")}
                       for anchor in original["anchors"] if anchor["purpose"] == "evidence"],
        })
    return result


def make_record(root=ROOT):
    paths = []

    def load(path):
        path = Path(path)
        paths.append(path)
        return read(path)

    dictionary = load(root / "target-dictionary.json")
    require(dictionary["source"].get("read_only") is True
            and dictionary["source"].get("target_written") is False, "Need real read-only dictionary snapshot")
    notes_raw = load(root / "review-notes-v3.json")
    require(notes_raw["source"]["dictionary_sha256"] == file_sha(root / "target-dictionary.json"),
            "Mapping notes target snapshot differs")
    notes = {(row["source_run"], row["group_id"]): row for row in notes_raw["records"]}
    records = []
    for name, folder, receipt_name, mapping_name in (
        ("baseline", SELECTED, "latest-receipt.json", "minimal-selected-mappings-v3.json"),
        ("e2e", E2E, "import-receipt.json", "minimal-e2e-mappings-v3.json"),
    ):
        bundle = load(folder / "bundle.json")
        source_notes = next((value for value in notes_raw["source"]["bundles"].values()
                             if value["source_run"] == bundle["source_run"]), {})
        require(source_notes.get("sha256") == file_sha(folder / "bundle.json"), "Mapping notes source bundle differs")
        exported = load(root / name / "tumor-api-export-v3.json")
        mappings = load(root / mapping_name)
        require(exported["mapping_sha256"] == digest(mappings), "Export mapping snapshot differs")
        receipt = load(folder / receipt_name)
        quality = quality_from_receipt(folder / "bundle.json", receipt, load(folder / "quality-input.json")) if name == "baseline" else None
        records.extend(build_batch(bundle, exported, receipt, notes, dictionary, quality))
    require(len(records) == len({row["id"] for row in records}) == 30, "Expected exactly 30 unique stored knowledge records")
    counts = Counter(row["compatibility_status"] for row in records)
    resolved = sum(sum(bool(item["accepted_by_exporter"]) for item in row["entity_mappings"]) for row in records)
    total = sum(len(row["entity_mappings"]) for row in records)
    exact = sum(sum(item["status"] == "exact_target_name" and item["accepted_by_exporter"] for item in row["entity_mappings"]) for row in records)
    proposed = sum(sum(item["status"] == "explicit_crosswalk" and item["accepted_by_exporter"] for item in row["entity_mappings"]) for row in records)
    ready = sum(row["offline_ready"] for row in records)
    ambiguous = sum(sum(item["status"] == "ambiguous" for item in row["entity_mappings"]) for row in records)
    source = dictionary["source"]
    summary = f"30条已提取知识：目标药物/癌种ID预览采用{resolved}/{total}项（{exact}项唯一精确匹配、{proposed}项本次提议癌种对应待核对），{ambiguous}项保留歧义；离线结构就绪{ready}/30。未导入tumor_api，结构缺口与知识质量分开。"
    for filename in ("scripts/publish_target_compatibility.py", "apps/curation/knowledge_export.py", "docs/research/022-target-compatibility.md"):
        paths.append(PROJECT / filename)
    record = {
        "schema_version": "research-history-v1", "id": RID,
        "title": "022 · 目标证据中心兼容核对（只读预览）",
        "date": datetime.fromisoformat(source["read_at_utc"]).astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "stage": "knowledge", "status": "evaluated",
        "task_kind": "target_compatibility", "sample_count": 30, "sample_unit": "条知识", "summary": summary,
        "task": {"question": "已有知识如何对应当前证据中心的真实实体ID、条件和治疗逻辑，还缺哪些适配字段？",
                 "input": "原5篇15条已有独立质量参考＋新5篇15条未核验知识，以及当前目标字典只读快照。",
                 "outputs": ["逐条真实药物/癌种UUID与对应依据", "目标trigger和多药逻辑、具体缺口", "原知识、文章与段落出处；既有质量结果独立展示"],
                 "not_measured": ["未调用模型或重新评判知识准确率", "未调用目标创建接口、未导入或写入tumor_api", "未验证临床适用性或覆盖tumor_api的全部旧知识库"]},
        "dataset": {"label": "两批已入biomed的30条知识", "role": "compatibility_preview", "document_count": 10,
                    "notes": ["本轮是结构适配核对，不是新模型实验。", "保留原知识内容与已有语义质量，不以适配失败判知识无效。"]},
        "reference": {"label": "当前证据中心契约与真实目标字典", "status": "snapshot",
                      "note": "范围为当前证据中心的临床证据和靶点规则；旧Diagnosis/Prognosis及GeneFunction不在本轮目标。零就绪不代表目标项目所有库均无法收纳。"},
        "methods": [{"id": VERSION, "label": "只读实体映射与离线结构检查", "version": VERSION,
                     "metrics": [{"label": "预览采用目标ID", "numerator": resolved, "denominator": total, "note": "包含本次提议的癌种对应，待用户核对；不是全部已确认，也不等于整条知识可导入。"},
                                 {"label": "唯一精确名称匹配", "value": exact, "note": "当前匹配规则在实际目标快照中唯一命中。"},
                                 {"label": "本次提议的主癌种对应", "value": proposed, "note": "保留原文分期/分子细分条件；仅预览，尚待用户核对。"},
                                 {"label": "离线结构就绪", "numerator": ready, "denominator": 30, "note": "仅目标结构契约检查，不是语义准确率或生产写入。"},
                                 *[{"label": key, "value": value, "note": "实际导出器逐条状态。"} for key, value in counts.items()]],
                     "conclusion": "保留完整biomed知识；缺trigger、逻辑或目标身份时只显示待适配，不补造条件或自动导入。"}],
        "findings": [summary, "多药联合、备选和序贯逻辑不得简单压成平铺药物ID列表。", "同名UUID有歧义时保持未决；不得以字符串相同自动选一个。"],
        "next_steps": ["按页面逐条核对真实目标ID与源文逻辑。", "依据缺口决定适配范围；本轮不安排目标写入或模型训练。"],
        "identity": {"task_version": VERSION, "sample_versions": [file_sha(SELECTED / "bundle.json"), file_sha(E2E / "bundle.json")],
                     "method_versions": [file_sha(PROJECT / "apps/curation/knowledge_export.py")], "reference_versions": [file_sha(root / "target-dictionary.json")], "completeness": "frozen"},
        "provenance": [{"label": path.name, "artifact": str(path.relative_to(PROJECT)), "sha256": file_sha(path)} for path in paths],
        "target_compatibility": {"preview_only": True, "target_written": False,
                                 "dictionary": {"snapshot_id": source["snapshot_id"], "captured_at": source["read_at_utc"],
                                                "counts": {kind: len(dictionary[kind]) for kind in ("drugs", "tumors", "rules")}},
                                 "records": records},
    }
    return prepare_record(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--register", action="store_true", help="Append the immutable research record after artifact checks")
    args = parser.parse_args()
    record = make_record(args.root)
    result = register_record(record) if args.register else {"status": "validated_not_registered", "id": RID}
    print(json.dumps({**result, "records": record["sample_count"], "summary": record["summary"], "target_written": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
