"""Track three known retry batches and a frozen classification experiment.

Read exactly three queue rows by primary key. Results are stored locally; this
monitor never sends mail, changes the queue, or starts a model.
"""

import argparse
import fcntl
import json
import signal
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.decision import digest
from apps.curation.knowledge_processing import STATE_ROOT
from apps.curation.research_history import register_record
from scripts.classify_literature import atomic_json
from scripts.prepare_classification_batch_study import DEFAULT_OUTPUT, file_hash
from scripts.prepare_knowledge_intake import freeze
from scripts.run_classification_batch_study import evaluate

BATCHES = (
    "kp-20261113T170624-38765bea",
    "kp-20261113T170433-33a87d06",
    "kp-20261113T170347-fa259890",
)
TERMINAL = {"completed", "failed", "superseded"}
STOP = False


def read(path):
    return json.loads(Path(path).read_text())


def fetch_batches(queue):
    connection = sqlite3.connect(
        Path(queue).resolve().as_uri() + "?mode=ro", uri=True, timeout=1
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT id,method,state,attempts,next_attempt,error,updated,summary "
            "FROM batches WHERE id IN (?,?,?)",
            BATCHES,
        ).fetchall()
    finally:
        connection.close()
    found = {row["id"]: row for row in rows}
    output = []
    for batch in BATCHES:
        row = found.get(batch)
        if row is None:
            output.append({"id": batch, "state": "missing"})
            continue
        item = {
            key: row[key]
            for key in ("id", "method", "state", "attempts", "next_attempt", "error")
        }
        if row["state"] in TERMINAL:
            item["updated"] = row["updated"]
        if row["state"] == "completed":
            summary = json.loads(row["summary"])
            articles = summary.get("articles", [])
            item["articles"] = len(articles)
            item["article_states"] = dict(
                Counter(article.get("queue_state", "unknown") for article in articles)
            )
            for key in (
                "knowledge_records",
                "structurally_eligible",
                "offline_ready_count",
            ):
                item[key] = summary.get(key, 0)
        output.append(item)
    return output


def snapshot(root, queue):
    root = Path(root)
    study = read(root / "study-status.json")
    result = {
        "study": {
            key: study.get(key)
            for key in ("state", "reason", "step", "matches", "count")
        },
        "batches": fetch_batches(queue),
    }
    audit = root / "retry-source-audit.json"
    if audit.exists():
        audit_data = read(audit)
        result["source_audit"] = [
            {"id": row["batch"], "state": row["status"]}
            for row in audit_data["batches"]
        ]
        result["source_audit_at_utc"] = audit_data.get("checked_at_utc")
        result["source_audit_sha256"] = file_hash(audit)
    if study.get("state") == "completed":
        saved = read(root / "comparison.json")
        verified = evaluate(root)
        if saved != verified:
            raise ValueError(
                "Saved comparison does not match the frozen per-paper results"
            )
        result["comparison"] = saved
    return result


def display_number(value, *, divisor=1, suffix=""):
    return "未测量" if value is None else f"{value / divisor:.2f}{suffix}"


def render(snapshot_value):
    study = snapshot_value["study"]
    lines = [
        "# 分类合批与三个重试批次",
        "",
        f"实验状态：{study['state']}；{study.get('reason') or '无额外等待原因'}。",
        "",
    ]
    report = snapshot_value.get("comparison")
    if report is None:
        lines += ["模型对照尚无完整、经过校验的结果；不推测分类一致性或资源变化。", ""]
    else:
        lines += [
            f"逐篇有效分类一致：{report['matches']}/{report['count']}。",
            (
                f"与 Astra 模型参考一致：拆批 {report['split_reference_matches']}/{report['count']}，"
                f"合批 {report['merged_reference_matches']}/{report['count']}。Astra 不是人工金标准。"
            ),
            "",
            "| 项目 | 两次各5篇 | 一次10篇 |",
            "| --- | ---: | ---: |",
        ]
        split, merged = report["split"], report["merged"]
        for label, key, divisor, unit in (
            ("模型加载", "model_loads", 1, " 次"),
            ("加载耗时", "load_seconds", 1, " 秒"),
            ("推理耗时", "inference_seconds", 1, " 秒"),
            ("进程总耗时", "wall_seconds", 1, " 秒"),
            ("显存峰值", "gpu_peak_mib", 1024, " GiB"),
            ("主机内存峰值", "host_peak_rss_kib", 1024**2, " GiB"),
            ("进程采样磁盘读取", "process_read_bytes_sampled", 1024**3, " GiB"),
        ):
            lines.append(
                f"| {label} | {display_number(split.get(key), divisor=divisor, suffix=unit)} | "
                f"{display_number(merged.get(key), divisor=divisor, suffix=unit)} |"
            )
        if not split.get("comparable_full_pass") or not merged.get(
            "comparable_full_pass"
        ):
            lines += [
                "",
                "有中断恢复或资源采样缺失；结果不能作为完整单次运行的直接资源对照。",
            ]
        lines += [
            "",
            *report["limitations"],
            "",
            "## 逐篇分类",
            "",
            "| PMID | Astra 参考 | 拆批 | 合批 |",
            "| --- | --- | --- | --- |",
        ]
        for case in report["cases"]:
            lines.append(
                f"| {case['pmid']} | {case['reference']} | {case['split'] or '未完成'} | {case['merged'] or '未完成'} |"
            )
        lines.append("")
    lines += ["## 三个重试批次", ""]
    for row in snapshot_value["batches"]:
        line = f"- {row['id']}：{row['state']}，尝试 {row.get('attempts', 0)} 次"
        if row["state"] == "completed":
            line += (
                f"；{row['articles']} 个文章条目，保存 {row['knowledge_records']} 条提取记录，"
                f"结构合格 {row['structurally_eligible']} 条，目标离线结构通过 {row['offline_ready_count']} 条"
            )
        if row.get("error"):
            line += f"；{row['error']}"
        lines.append(line + "。")
    lines += [
        "",
        "条目数与结构检查不等于去重后、经临床核验的有效知识；不写入 tumor_api。",
        "",
    ]
    return "\n".join(lines)


def publish_retry_record(root, snapshot_value):
    root = Path(root)
    rows = snapshot_value["batches"]
    if not all(row["state"] in TERMINAL for row in rows):
        return None
    terminal = {
        "batches": rows,
        "source_audit": snapshot_value.get("source_audit", []),
        "source_audit_at_utc": snapshot_value.get("source_audit_at_utc"),
        "source_audit_sha256": snapshot_value.get("source_audit_sha256"),
    }
    revision = digest(terminal)[:12]
    final = root / f"retry-final-{revision}.json"
    freeze(final, terminal)
    finished = max(row["updated"] for row in rows)
    day = datetime.fromtimestamp(finished, ZoneInfo("Asia/Shanghai")).date().isoformat()
    complete = sum(row["state"] == "completed" for row in rows)
    failed = sum(row["state"] == "failed" for row in rows)
    facts = sum(row.get("knowledge_records", 0) for row in rows)
    methods = sorted({row["method"] for row in rows})
    findings = [
        f"{row['id']}：{row['state']}；尝试 {row['attempts']} 次；"
        f"文章去向 {json.dumps(row.get('article_states', {}), ensure_ascii=False)}；"
        f"记录 {row.get('knowledge_records', 0)} 条，目标离线结构通过 {row.get('offline_ready_count', 0)} 条。"
        for row in rows
    ]
    record = {
        "schema_version": "research-history-v1",
        "id": f"knowledge-retry-followup-20261114-{revision}",
        "title": "资料 worker 恢复后的三个批次",
        "date": day,
        "stage": "knowledge",
        "status": "evaluated",
        "sample_count": 15,
        "summary": f"三个重试批次中 {complete} 个完成、{failed} 个失败；来源及处理结果保留。",
        "task": {
            "question": "资料 worker 恢复后，三个来源失败批次是否按原断点完成？",
            "input": "原队列三个批次、各 5 篇冻结来源。",
            "outputs": ["批次状态", "提取记录", "目标离线结构通过数"],
            "not_measured": ["全批语义正确率", "生产 tumor_api 导入"],
        },
        "dataset": {
            "label": "三个已存在的重试批次",
            "role": "operational",
            "notes": ["不重置尝试次数，不修改来源或原队列。"],
        },
        "reference": {
            "label": "批次账本和来源哈希检查",
            "status": "operational_record",
            "note": "结构结果不等同医学正确性。",
        },
        "identity": {
            "task_version": "knowledge-retry-followup-v1",
            "sample_versions": [file_hash(final)],
            "method_versions": methods or ["unknown"],
            "reference_versions": [
                terminal["source_audit_sha256"] or digest(terminal["source_audit"])
            ],
            "completeness": "frozen",
        },
        "methods": [
            {
                "id": "existing-retry",
                "label": "原队列重试与来源验证",
                "version": ", ".join(methods) or "unknown",
                "metrics": [
                    {
                        "label": "批次完成",
                        "numerator": complete,
                        "denominator": 3,
                        "note": "批次完成不等于每篇知识完整。",
                    },
                    {
                        "label": "批次失败",
                        "value": failed,
                        "unit": "个",
                        "note": "保留原失败原因与尝试记录。",
                    },
                    {
                        "label": "保存提取记录",
                        "value": facts,
                        "unit": "条",
                        "note": "未经语义去重，不等同独立有效知识。",
                    },
                ],
                "conclusion": "继续沿用原有断点和限速。",
            }
        ],
        "findings": findings,
        "next_steps": ["按实际 partial 或 failed 的原因继续处理，不重复覆盖既有结果。"],
        "provenance": [
            {
                "label": "三个批次终态",
                "artifact": str(DEFAULT_OUTPUT / final.name),
                "sha256": file_hash(final),
            }
        ],
    }
    if terminal["source_audit_sha256"]:
        record["provenance"].append(
            {
                "label": "来源复查",
                "artifact": str(DEFAULT_OUTPUT / "retry-source-audit.json"),
                "sha256": terminal["source_audit_sha256"],
            }
        )
    return register_record(record)


def refresh(root, queue):
    root = Path(root)
    with (root / "followup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = snapshot(root, queue)
        path = root / "followup-status.json"
        previous = read(path) if path.exists() else {"history": []}
        changed = previous.get("current") != current
        report_path = root / "followup-report.md"
        if changed or not report_path.exists():
            temporary = root / ".followup-report.tmp"
            temporary.write_text(render(current), encoding="utf-8")
            temporary.replace(report_path)
        if changed:
            history = previous.get("history", [])
            history.append(
                {
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                    "study": current["study"],
                    "batches": [
                        {
                            "id": row["id"],
                            "state": row["state"],
                            "attempts": row.get("attempts"),
                        }
                        for row in current["batches"]
                    ],
                }
            )
            atomic_json(path, {"current": current, "history": history})
        if all(row["state"] in TERMINAL for row in current["batches"]):
            publish_retry_record(root, current)
        return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--queue", type=Path, default=STATE_ROOT / "queue.sqlite3")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--interval", type=int, default=600)
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")

    def stop(signum, frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not STOP:
        delay = args.interval
        try:
            current = refresh(args.root, args.queue)
            print(
                json.dumps(
                    {
                        "study": current["study"]["state"],
                        "batches": [row["state"] for row in current["batches"]],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if current["study"]["state"] == "completed" and all(
                row["state"] in TERMINAL for row in current["batches"]
            ):
                return 0
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            print(
                f"Follow-up temporarily unavailable: {type(exc).__name__}: {str(exc)[:150]}",
                file=sys.stderr,
                flush=True,
            )
            if not args.wait:
                return 1
            delay = 60
        if not args.wait:
            return 0
        remaining = delay
        while remaining > 0 and not STOP:
            chunk = min(30, remaining)
            time.sleep(chunk)
            remaining -= chunk
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
