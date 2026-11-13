"""Versioned, read-only research records; independent of the collection database."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "research-history-v1"
DIRECTORY = Path(__file__).parent / "research_data"
PUBLIC_DIRECTORY = Path(__file__).parent / "research_public_data"
# A completed ten-paper batch with evidence and target previews exceeds 4 MiB.
# Keep a bounded local artifact; the UI renders compatibility rows in pages.
MAX_BYTES = 8 * 1024 * 1024
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,100}")
CURRENT_TASK = {
    "id": "literature-three-class-v1",
    "title": "当前方向：文献三分类",
    "status": "planned",
    "categories": ["实体瘤", "血液肿瘤", "非肿瘤"],
    "note": "用户已确定只保留三种业务分类。旧 9 分类结果作为历史记录；三分类尚未实测。"
    "输出失败或信息不足记为未完成状态，不是第四种分类。",
}
STAGES = [
    {
        "id": "classification",
        "label": "文献筛选与分类",
        "status": "evaluated",
        "note": "已比较规则与小模型；当前结果来自开发样本。",
    },
    {
        "id": "passages",
        "label": "候选证据段落",
        "status": "unscored",
        "note": "已定位候选段落，尚无全文证据参考，不能报告准确率。",
    },
    {
        "id": "entities",
        "label": "实体与关系",
        "status": "not_run",
        "note": "尚未评测；疾病、药物等标签不能代替实体关系抽取。",
    },
    {
        "id": "knowledge",
        "label": "知识验证与入库",
        "status": "not_run",
        "note": "尚未完成可发布知识闭环，未训练或发布实验结果。",
    },
]


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _validate(record):
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported research record")
    if not isinstance(record.get("id"), str) or not ID_PATTERN.fullmatch(record["id"]):
        raise ValueError("Invalid research ID")
    for field in ("title", "date", "summary"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"Missing research {field}")
    if record.get("stage") not in {stage["id"] for stage in STAGES}:
        raise ValueError("Unknown research stage")
    if record.get("status") not in {"evaluated", "unscored", "planned"}:
        raise ValueError("Unknown research status")
    count = record.get("sample_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Invalid research sample count")
    for field in ("task", "dataset", "reference", "identity"):
        if not isinstance(record.get(field), dict):
            raise TypeError(f"Missing research {field}")
    for field in ("methods", "findings", "next_steps", "provenance"):
        if not isinstance(record.get(field), list):
            raise TypeError(f"Missing research {field}")
    identity = record["identity"]
    if identity.get("completeness") not in {"frozen", "documented_only"}:
        raise ValueError("Unknown identity completeness")
    if not isinstance(identity.get("task_version"), str) or not identity["task_version"]:
        raise ValueError("Missing task version")
    for field in ("sample_versions", "method_versions", "reference_versions"):
        if not isinstance(identity.get(field), list) or not identity[field]:
            raise ValueError(f"Missing identity {field}")
        if any(not isinstance(value, str) or not value for value in identity[field]):
            raise ValueError(f"Invalid identity {field}")
    for method in record["methods"]:
        for metric in method["metrics"]:
            if "numerator" in metric or "denominator" in metric:
                numerator = metric.get("numerator")
                denominator = metric.get("denominator")
                if (
                    type(numerator) is not int
                    or type(denominator) is not int
                    or not 0 <= numerator <= denominator
                    or denominator == 0
                ):
                    raise ValueError("Invalid metric denominator")


def prepare_record(record):
    """Content and experiment identities serve different purposes."""
    record = {key: value for key, value in record.items() if key != "content_sha256"}
    _validate(record)
    fingerprint = _hash(record["identity"])
    if record.get("fingerprint", fingerprint) != fingerprint:
        raise ValueError("Research identity changed")
    record["fingerprint"] = fingerprint
    record["content_sha256"] = _hash(record)
    return record


def read_record(record_id, directory=DIRECTORY):
    if not isinstance(record_id, str) or not ID_PATTERN.fullmatch(record_id):
        raise FileNotFoundError("Unknown research record")
    path = Path(directory) / f"{record_id}.json"
    if path.is_symlink():
        raise ValueError("Research records cannot be symlinks")
    if not path.exists() and Path(directory) == DIRECTORY:
        path = PUBLIC_DIRECTORY / f"{record_id}.json"
        if path.is_symlink():
            raise ValueError("Research records cannot be symlinks")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Research record is too large")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("id") != record_id:
        raise ValueError("Research filename and identity differ")
    checked = prepare_record(record)
    if record.get("content_sha256") != checked["content_sha256"]:
        raise ValueError("Research content checksum mismatch")
    return checked


def public_record(record):
    """Keep frozen files intact while presenting portable artifact references."""
    project = Path(__file__).resolve().parents[2]
    provenance = []
    for source in record["provenance"]:
        item = dict(source)
        artifact = Path(item.get("artifact", ""))
        if artifact.is_absolute():
            try:
                item["artifact"] = str(artifact.relative_to(project))
            except ValueError:
                item["artifact"] = artifact.name
        provenance.append(item)
    return prepare_record({**record, "provenance": provenance})


def register_record(record, directory=DIRECTORY):
    """Append an immutable projection. Reusing an ID never overwrites its results."""
    record = prepare_record(record)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['id']}.json"
    if path.exists():
        previous = read_record(record["id"], directory)
        if previous != record:
            raise ValueError("Research ID already exists; append a new version")
        return {"status": "reused", "id": record["id"], "fingerprint": record["fingerprint"]}
    payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(payload.encode()) > MAX_BYTES:
        raise ValueError("Research record is too large")
    # A completed temporary file is linked exclusively; readers cannot see a half-write.
    descriptor, temporary = tempfile.mkstemp(prefix=".record-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, path)
        except FileExistsError:
            previous = read_record(record["id"], directory)
            if previous != record:
                raise ValueError("Research ID already exists; append a new version") from None
            return {"status": "reused", "id": record["id"], "fingerprint": record["fingerprint"]}
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"status": "created", "id": record["id"], "fingerprint": record["fingerprint"]}


def _record_ids(directory):
    directories = [Path(directory)]
    if Path(directory) == DIRECTORY:
        directories.append(PUBLIC_DIRECTORY)
    return {path.stem for root in directories for path in root.glob("*.json")}


def records_index(directory=DIRECTORY):
    rows = [read_record(record_id, directory) for record_id in _record_ids(directory)]
    stage_order = {stage["id"]: index for index, stage in enumerate(STAGES)}
    # Date-only historical records have no finer ordering. Within the same date,
    # prefer the furthest pipeline stage, then retain the ID tie-break.
    def chronology(row):
        recorded = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
        recorded = recorded.replace(tzinfo=timezone.utc) if recorded.tzinfo is None else recorded
        return recorded.timestamp(), stage_order[row["stage"]], row["id"]

    rows.sort(key=chronology, reverse=True)
    fields = ("id", "title", "date", "stage", "status", "sample_count", "summary")
    latest = next((row for row in rows if row.get("task_kind") in {
        "three_class", "passage_screening", "knowledge_candidates", "entity_resolution", "target_compatibility",
    }), None)
    current_task = dict(CURRENT_TASK)
    stages = [dict(stage) for stage in STAGES]
    for stage in stages:
        result = next((row for row in rows if row["stage"] == stage["id"]), None)
        if result:
            stage.update(status=result["status"], note=result["summary"])
    if latest and latest.get("task_kind") == "target_compatibility":
        current_task.update(
            id="literature-knowledge-target-compatibility-v1", title="当前方向：文献知识与目标兼容",
            status=latest["status"], categories=["批量提取", "实体归一", "两层出处", "目标兼容"],
            note=latest["summary"] + " 知识质量独立核验；离线结构通过不等于目标生产导入。",
        )
    elif latest and latest.get("task_kind") == "entity_resolution":
        current_task.update(
            id="local-entity-resolution-v1", title="当前方向：本地实体库与名称对齐",
            status=latest["status"], categories=["实体归一", "别名复用", "主癌种／亚型", "文献新增"],
            note=latest["summary"] + " 原知识及限定条件保留，后续运行只使用本地实体库。",
        )
    elif latest and latest.get("task_kind") == "knowledge_candidates" and latest["stage"] == "knowledge":
        current_task.update(
            id="source-knowledge-publication-v1",
            title="当前方向：知识核验、发布与检索",
            status=latest["status"],
            categories=["核验", "发布", "检索", "两层出处"],
            note=latest["summary"] + " AI核验为模型参考，人工抽查独立；"
            "已发布范围为biomed文献知识，尚未接入tumor_api生产报告。",
        )
    elif latest and latest.get("task_kind") == "knowledge_candidates":
        papers = latest["dataset"].get("document_count")
        scope = f"{papers} 篇文献、" if papers else ""
        current_task.update(
            id="solid-treatment-knowledge-candidates-v1",
            title="当前方向：实体、关系与条件抽取",
            status=latest["status"],
            categories=["实体", "关系", "条件", "两层出处"],
            note=f"{scope}{latest['sample_count']} 条知识候选试验已记录；"
            "输入来自上一步筛选的有效片段，分别核对文章出处、原文片段与目标结构映射。"
            "Astra 仅作模型参考，实验候选尚未发布为生产证据。",
        )
    elif latest and latest.get("task_kind") == "passage_screening":
        papers = latest["dataset"].get("document_count")
        scope = f"{papers} 篇文献、" if papers else ""
        current_task.update(
            id="solid-treatment-passage-screening-v1",
            title="当前方向：治疗证据段落筛选",
            status=latest["status"],
            categories=["相关", "不相关"],
            note=f"{scope}{latest['sample_count']} 个已解析段落已完成筛选评测；信息不足单列为待核查。"
            "文献三分类已选定 Qwen3.5-4B；段落相关不等于已发布临床证据。",
        )
    elif latest:
        current_task.update(
            status=latest["status"],
            note=f"{latest['sample_count']} 篇三分类试验已完成。研究记录只保留任务、方法、结果、关键错误与下一步；"
            "原始输入和中间过程留在来源记录中。",
        )
    preferred = latest["id"] if latest else next(
        (record_id for record_id in ("three-class-20261022", "scope-pilot-20261022")
         if any(row["id"] == record_id for row in rows)),
        rows[0]["id"] if rows else None,
    )
    return {
        "schema_version": SCHEMA,
        "current_task": current_task,
        "stages": stages,
        "experiments": [
            {**{key: row[key] for key in fields}, "sample_unit": row.get("sample_unit", "篇")}
            for row in rows
        ],
        "default_experiment": preferred,
    }


def find_identity(identity, directory=DIRECTORY):
    """Preflight lookup, not an inference scheduler or proof of an independent test."""
    fingerprint = _hash(identity)
    matches = []
    for record_id in _record_ids(directory):
        row = read_record(record_id, directory)
        if row["fingerprint"] == fingerprint:
            matches.append({"id": row["id"], "title": row["title"], "date": row["date"]})
    return {"fingerprint": fingerprint, "matches": matches}
