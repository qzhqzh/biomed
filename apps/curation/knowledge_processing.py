"""Durable host-side knowledge queue. Production sources are only read here."""

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.pubtator import atomic_json, read

ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = Path("/appdata/02_resource/apps/biomed/operations/knowledge-processing")
DATABASE = Path("/appdata/02_resource/apps/biomed/production/var/django/db.sqlite3")
LIBRARY = DATABASE.parent.parent / "library"
VERSION = "knowledge-service-v1"
PARSER = "text-v1-pypdf-6.20.0-jats-1"
TERMINAL = {"completed", "partial", "excluded", "failed", "legacy", "source_notice"}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def utc_day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).date().isoformat()


def pipeline_method(clinical_profile=None):
    # Operating budgets are independent of extraction identity. Frozen methods
    # include prompts, adapters and model manifests, never mutable entity rules.
    names = ["scripts/run_literature_batch.py", "scripts/run_minimal_pipeline.py",
             "scripts/prepare_minimal_e2e.py", "scripts/light_entity_pilot.py",
             "apps/curation/minimal_knowledge.py", "apps/curation/knowledge_eligibility.py",
             "apps/curation/compound_entity_mentions.py", "apps/curation/knowledge_relation_prompt.py",
             "apps/curation/knowledge_target_mapping.py", "apps/curation/entity_normalization.py",
             "var/experiments/three-class-20261022/task-definition-v1.json",
             "var/experiments/lean-entities-20261029/definition.json"]
    files = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    if clinical_profile:
        if clinical_profile != "target-clinical-focus-v1":
            raise ValueError("Unknown clinical profile")
        for name in ("apps/curation/knowledge_relation_clinical_prompt.py",
                     "apps/curation/knowledge_screening_clinical_prompt.py",
                     "apps/curation/knowledge_clinical_mapping.py", "apps/curation/knowledge_export.py",
                     "apps/curation/knowledge_source_priority.py", "data/knowledge/genes.jsonl"):
            files[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        files["clinical_profile"] = clinical_profile
    for name in ("Qwen3.5-4B", "Qwen3.5-9B"):
        path = Path("/data/models") / name / "download-manifest.json"
        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"version": VERSION, "parser": PARSER, "files": files, "id": identity(files),
            "clinical_profile": clinical_profile}


def source_page(database=DATABASE, after="", limit=256, *, lock_timeout=.5):
    """Use the small parsed-passage index, not a scan of millions of metadata rows.

    Every SELECT releases its read lock. No transaction survives between calls,
    and both lock waits and query CPU time are bounded.
    """
    if not isinstance(lock_timeout, (int, float)) or isinstance(lock_timeout, bool) or not 0 < lock_timeout <= 5:
        raise ValueError("Source lock timeout must be greater than 0 and at most 5 seconds")
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=lock_timeout)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")

        def query(sql, args=()):
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            try:
                return [dict(row) for row in db.execute(sql, args).fetchall()]
            finally:
                db.set_progress_handler(None, 0)

        ids = query("SELECT DISTINCT version_id FROM curation_passage WHERE version_id>? ORDER BY version_id LIMIT ?", (after, limit))
        rows = []
        for item in ids:
            found = query("""SELECT v.id AS version_id,v.sha256,m.id AS material_id,m.title,m.identifiers,m.created_at,
                (SELECT count(*) FROM curation_passage p WHERE p.version_id=v.id AND parser_version=?) AS passages,
                EXISTS(SELECT 1 FROM curation_knowledgecandidate k WHERE k.version_id=v.id) AS has_knowledge
                FROM curation_materialversion v JOIN curation_material m ON m.id=v.material_id
                WHERE v.id=? AND v.media_type='application/jats+xml' AND v.integrity_status='valid' AND m.kind='paper'""",
                          (PARSER, item["version_id"]))
            if not found or not found[0]["passages"]:
                continue
            row = found[0]
            ids_value = json.loads(row.pop("identifiers"))
            row["pmid"] = str(ids_value.get("pubmed", ids_value.get("pmid", "")))
            if not re.fullmatch(r"[1-9]\d{0,9}", row["pmid"]):
                continue
            from apps.curation.knowledge_source_priority import rank_source
            # Indexed and bounded: rank existing JATS only, never scan metadata
            # or use a keyword hit as a target-ready knowledge assertion.
            text_deferred = False
            try:
                snippets = query("""SELECT locator,text FROM curation_passage
                    WHERE version_id=? AND parser_version=? ORDER BY ordinal LIMIT 16""", (row["version_id"], PARSER))
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                snippets, text_deferred = [], True
            row["priority_reason"] = rank_source(row["title"], snippets)
            row["priority_reason"]["text_deferred"] = text_deferred
            row["priority"] = row["priority_reason"]["priority"]
            rows.append(row)
        return rows, ids[-1]["version_id"] if len(ids) == limit else ""


class Queue:
    def __init__(self, root=STATE_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "queue.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        # Short transactions in this single-worker ledger do not need WAL.
        # Keep the rollback-journal policy used by the existing host deployment.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, method TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                seconds REAL NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '{}',
                pid INTEGER, process_start TEXT);
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY, version_id TEXT NOT NULL, method TEXT NOT NULL,
                pmid TEXT NOT NULL, material_id TEXT NOT NULL, source TEXT NOT NULL,
                state TEXT NOT NULL, priority INTEGER NOT NULL, batch_id TEXT REFERENCES batches(id),
                discovered REAL NOT NULL, updated REAL NOT NULL, outcome TEXT NOT NULL DEFAULT '{}',
                UNIQUE(version_id,method));
            CREATE INDEX IF NOT EXISTS papers_ready ON papers(state,priority,discovered);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, batch_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY,articles INTEGER NOT NULL DEFAULT 0,seconds REAL NOT NULL DEFAULT 0);
        """)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def event(self, batch, kind, detail, now=None):
        self.db.execute("INSERT INTO events(at,batch_id,kind,detail) VALUES (?,?,?,?)",
                        (time.time() if now is None else now, batch, kind, json.dumps(detail, ensure_ascii=False)))

    def discover(self, rows, method, now=None):
        now = time.time() if now is None else now
        added = 0
        with self.db:
            for row in rows:
                previous = self.db.execute("SELECT source FROM papers WHERE version_id=?", (row["version_id"],)).fetchall()
                if any(json.loads(item[0])["sha256"] == row["sha256"] for item in previous):
                    # Refresh ordering only for unclaimed work. Preserve its
                    # source snapshot, method, discovery time and all results.
                    if not row.get('priority_reason', {}).get('text_deferred'):
                        self.db.execute("""UPDATE papers SET priority=? WHERE version_id=?
                            AND state='queued' AND batch_id IS NULL""", (row['priority'], row['version_id']))
                    # A method upgrade is not authorization to duplicate past
                    # extraction. Explicit research replays use separate runs.
                    continue
                # Any old knowledge is explicit legacy coverage, not a new
                # success, and is never silently re-extracted on a new run.
                state = "legacy" if row["has_knowledge"] else "queued"
                if re.match(r"^(correction\b|retraction\b|retracted\b|\[retracted\]|erratum\b)", row["title"], re.IGNORECASE):
                    state = "source_notice"
                key = identity([row["version_id"], row["sha256"], method])
                changed = self.db.execute("""INSERT OR IGNORE INTO papers
                    (id,version_id,method,pmid,material_id,source,state,priority,discovered,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (key, row["version_id"], method, row["pmid"], row["material_id"],
                    json.dumps(row, ensure_ascii=False), state, row["priority"], now, now)).rowcount
                added += changed
            if added:
                self.event(None, "discovered", {"added": added}, now)
        return added

    def upgrade_pending_method(self, method, now=None):
        """Move only unstarted items to a new method, preserving usage/history."""
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM batches WHERE state IN ('ready','retry','running') AND method<>? LIMIT 1", (method,)).fetchone():
                self.db.commit()
                return 0
            rows = self.db.execute("SELECT * FROM papers WHERE state='queued' AND batch_id IS NULL AND method<>?", (method,)).fetchall()
            for row in rows:
                source = json.loads(row["source"])
                key = identity([row["version_id"], source["sha256"], method])
                self.db.execute("UPDATE papers SET id=?,method=?,updated=? WHERE id=?", (key, method, now, row["id"]))
                self.event(None, "pending_method_upgraded", {"old_id": row["id"], "new_id": key,
                    "old_method": row["method"], "new_method": method, "version_id": row["version_id"]}, now)
            self.db.commit()
            return len(rows)
        except BaseException:
            self.db.rollback()
            raise

    def usage(self, now=None):
        row = self.db.execute("SELECT articles,seconds FROM usage WHERE day=?", (utc_day(now),)).fetchone()
        return dict(row) if row else {"articles": 0, "seconds": 0}

    def next_batch(self, method, size, daily_articles, daily_seconds, now=None, minimum_priority=0):
        """Claim work; a None daily limit is unlimited, without resetting usage."""
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            used = self.usage(now)
            if self.get("paused", False) or (daily_seconds is not None and used["seconds"] >= daily_seconds):
                self.db.commit()
                return None
            row = self.db.execute("SELECT * FROM batches WHERE state IN ('ready','retry') AND next_attempt<=? ORDER BY created LIMIT 1", (now,)).fetchone()
            if row:
                self.db.commit()
                return dict(row)
            limit = size if daily_articles is None else min(size, max(0, daily_articles - used["articles"]))
            papers = self.db.execute("""SELECT id FROM papers WHERE state='queued' AND method=? AND priority>=?
                ORDER BY priority DESC,discovered,pmid LIMIT ?""", (method, minimum_priority, limit)).fetchall()
            if not papers:
                self.db.commit()
                return None
            batch = "kp-" + datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + papers[0][0][:8]
            self.db.execute("INSERT INTO batches(id,method,state,created,updated) VALUES (?,?,?,?,?)", (batch, method, "ready", now, now))
            self.db.executemany("UPDATE papers SET state='assigned',batch_id=?,updated=? WHERE id=?", [(batch, now, p[0]) for p in papers])
            self.db.execute("INSERT INTO usage(day,articles) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET articles=articles+excluded.articles", (utc_day(now), len(papers)))
            self.event(batch, "created", {"papers": len(papers)}, now)
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone())
        except BaseException:
            self.db.rollback()
            raise

    def papers(self, batch):
        return [json.loads(r[0]) for r in self.db.execute("SELECT source FROM papers WHERE batch_id=? ORDER BY pmid", (batch,))]

    def start(self, batch, now=None):
        now = time.time() if now is None else now
        with self.db:
            changed = self.db.execute("UPDATE batches SET state='running',attempts=attempts+1,updated=?,error='' WHERE id=? AND state IN ('ready','retry')", (now, batch)).rowcount
            if changed != 1:
                raise ValueError("Batch is not claimable")
            self.db.execute("UPDATE papers SET state='running',updated=? WHERE batch_id=?", (now, batch))
            self.event(batch, "started", {}, now)

    def process(self, batch, pid, token):
        with self.db:
            self.db.execute("UPDATE batches SET pid=?,process_start=? WHERE id=?", (pid, token, batch))

    def charge(self, batch, seconds, now=None):
        if seconds < 0:
            raise ValueError("Negative runtime")
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("INSERT INTO usage(day,seconds) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds", (utc_day(now), seconds))
            self.db.execute("UPDATE batches SET seconds=seconds+?,updated=? WHERE id=?", (seconds, now, batch))

    def failure(self, batch, message, *, interrupted=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT attempts FROM batches WHERE id=?", (batch,)).fetchone()
            exhausted = row[0] >= 3 and not interrupted
            state = "failed" if exhausted else "retry"
            delay = 0 if interrupted else (60, 300, 900)[min(row[0] - 1, 2)]
            self.db.execute("UPDATE batches SET state=?,updated=?,next_attempt=?,error=?,pid=NULL,process_start=NULL,attempts=attempts-? WHERE id=?",
                            (state, now, now + delay, message[:500], int(interrupted), batch))
            self.db.execute("UPDATE papers SET state=?,updated=?,outcome=? WHERE batch_id=?",
                            ("failed" if exhausted else "assigned", now, json.dumps({"reason": message[:500]}), batch))
            self.event(batch, "interrupted" if interrupted else state, {"reason": message[:500], "retry_at": now + delay}, now)
        if exhausted:
            count = self.get("consecutive_failures", 0) + 1
            self.set("consecutive_failures", count)
            if count >= 3:
                self.set("paused", True)
                self.set("pause_reason", "连续3个批次耗尽重试，需检查运行环境")

    def complete(self, batch, summary, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE batches SET state='completed',summary=?,updated=?,pid=NULL,process_start=NULL,error='' WHERE id=?",
                            (json.dumps(summary, ensure_ascii=False), now, batch))
            for article in summary["articles"]:
                state = article["queue_state"]
                if state not in TERMINAL:
                    raise ValueError("Invalid article outcome")
                self.db.execute("UPDATE papers SET state=?,updated=?,outcome=? WHERE batch_id=? AND pmid=?",
                                (state, now, json.dumps(article, ensure_ascii=False), batch, article["pmid"]))
            if self.db.execute("SELECT count(*) FROM papers WHERE batch_id=? AND state='running'", (batch,)).fetchone()[0]:
                raise ValueError("Missing paper outcomes")
            self.event(batch, "completed", {k: v for k, v in summary.items() if k != "articles"}, now)
        self.set("consecutive_failures", 0)

    def status(self, activity, *, config, progress=None):
        rows = [dict(r) for r in self.db.execute("SELECT id,state,attempts,seconds,created,updated,error,summary,next_attempt FROM batches ORDER BY created DESC LIMIT 20")]
        totals = Counter()
        for row in self.db.execute("SELECT summary FROM batches WHERE state='completed'"):
            summary = json.loads(row[0])
            totals.update({k: summary.get(k, 0) for k in ("knowledge_records", "structurally_eligible", "offline_ready_count", "reviewed_records")})
        for row in rows:
            row["summary"] = json.loads(row["summary"])
        return {"version": VERSION, "updated_at": time.time(), "activity": activity,
                "paused": self.get("paused", False), "pause_reason": self.get("pause_reason", ""),
                "papers": dict(self.db.execute("SELECT state,count(*) FROM papers GROUP BY state").fetchall()),
                "totals": dict(totals), "today": self.usage(), "limits": config,
                "progress": progress, "batches": rows, "quality_note": "结构检查与语义正确率分开；未核验不计为医学正确。"}

    def refresh_target_readiness(self, batch):
        """Append a derived target diagnosis without changing import or review data."""
        from apps.curation.knowledge_target_readiness import readiness_summary

        row = self.db.execute("SELECT summary FROM batches WHERE id=? AND state='completed'", (batch,)).fetchone()
        if row is None:
            raise ValueError("Reassess only a completed batch")
        folder = self.root / "batches" / batch
        source = folder / "final/knowledge.json"
        receipt = read(folder / "final/import-receipt.json")
        if (receipt.get("bundle_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()
                or receipt.get("result", {}).get("dry_run") or "entity_registry" not in receipt.get("result", {})):
            raise ValueError("Target diagnosis requires the original verified import receipt")
        report = readiness_summary(read(source)["records"])
        report["knowledge_sha256"] = receipt["bundle_sha256"]
        key = identity(report)
        path = folder / "target-readiness" / f"{key}.json"
        if not path.exists():
            atomic_json(path, report)
        with self.db:
            # Read current summary inside the short write transaction so an
            # independently added quality review is not replaced by stale data.
            self.db.execute("BEGIN IMMEDIATE")
            summary = json.loads(self.db.execute("SELECT summary FROM batches WHERE id=?", (batch,)).fetchone()[0])
            summary["target_readiness"] = report
            self.db.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps(summary, ensure_ascii=False), batch))
            detail = json.dumps({"id": key}, ensure_ascii=False)
            if not self.db.execute("SELECT 1 FROM events WHERE batch_id=? AND kind='target_reassessed' AND detail=?", (batch, detail)).fetchone():
                self.event(batch, "target_reassessed", {"id": key})
        return {"id": key, "counts": report["counts"], "structurally_eligible": report["structurally_eligible"]}

    def record_evaluation(self, batch, payload):
        """Append an independent sample review; never edit knowledge or gate intake."""
        row = self.db.execute("SELECT summary FROM batches WHERE id=? AND state='completed'", (batch,)).fetchone()
        if row is None:
            raise ValueError("Evaluate only a completed batch")
        folder = self.root / "batches" / batch
        sample = read(folder / "evaluation-sample.json")
        if payload.get("source_run") != batch or payload.get("sample_sha256") != identity(sample):
            raise ValueError("Evaluation refers to a different sample")
        if not payload.get("reviewer") or not isinstance(payload["reviewer"], str):
            raise ValueError("Record the actual reviewer, not a generated reference label")
        expected = {r["group_id"]: r for r in sample["records"]}
        rows = payload.get("records", [])
        if not expected or len(rows) != len(expected) or {r.get("group_id") for r in rows} != set(expected):
            raise ValueError("Review every fixed sample record exactly once")
        counts, issues = Counter(), Counter()
        for result in rows:
            if result.get("decision") not in {"supported", "unsupported", "uncertain"} or not result.get("reason"):
                raise ValueError("Review needs a decision and an explanation")
            quotes = {a["quote"] for a in expected[result["group_id"]].get("anchors", [])}
            missing_source = not quotes and result["decision"] != "supported" and "missing_source_anchor" in result.get("issue_codes", [])
            if not missing_source and (not result.get("evidence_quotes") or any(q not in quotes for q in result["evidence_quotes"])):
                raise ValueError("Review must quote the fixed source anchors")
            if not isinstance(result.get("issue_codes", []), list) or any(not isinstance(code, str) for code in result.get("issue_codes", [])):
                raise ValueError("Issue codes must be strings")
            counts[result["decision"]] += 1
            issues.update(result.get("issue_codes", []))
        lessons = payload.get("lessons", [])
        if not isinstance(lessons, list) or len(lessons) > 3 or any(not isinstance(item, str) for item in lessons):
            raise ValueError("Keep at most three concrete lessons")
        key = identity(payload)
        path = folder / "evaluations" / f"{key}.json"
        if not path.exists():
            atomic_json(path, payload)
        elif read(path) != payload:
            raise ValueError("Conflicting evaluation replay")
        summary = json.loads(row[0])
        summary.update(reviewed_records=len(rows), quality_status="reference_sample_reviewed",
                       evaluation={"id": key, "reviewer": payload["reviewer"], "counts": dict(counts),
                                   "issues": dict(issues), "lessons": lessons,
                                   "note": "固定小样本原文核读，非全批准确率；不改变提取、入库或发布状态。"})
        with self.db:
            self.db.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps(summary, ensure_ascii=False), batch))
            if not self.db.execute("SELECT 1 FROM events WHERE batch_id=? AND kind='evaluated' AND detail=?", (batch, json.dumps({"id": key}, ensure_ascii=False))).fetchone():
                self.event(batch, "evaluated", {"id": key})
        atomic_json(folder / "evaluation-summary.json", summary["evaluation"])
        return summary["evaluation"]


def batch_report(folder):
    """Require real import receipts. Summaries never assert semantic correctness."""
    folder = Path(folder)
    no_solid = folder / "batch-result.json"
    final = folder / "final"
    if no_solid.exists() and read(no_solid).get("state") == "no_solid_articles":
        result, records = read(no_solid), []
    else:
        result, bundle = read(final / "summary.json"), read(final / "knowledge.json")
        receipt = read(final / "import-receipt.json")
        if (receipt.get("bundle_sha256") != hashlib.sha256((final / "knowledge.json").read_bytes()).hexdigest()
                or receipt.get("result", {}).get("dry_run") or "entity_registry" not in receipt.get("result", {})):
            raise ValueError("Knowledge has no verified biomed import receipt")
        records = bundle["records"]
    articles = []
    for original in result["articles"]:
        row = dict(original)
        category = row.get("effective_category", row.get("category"))
        extraction = row.get("extraction", {})
        if category in ("non_neoplastic", "hematologic"):
            state = "excluded"
        elif category != "solid" or row.get("effective_status", row.get("status")) != "classified":
            state = "failed"
        elif extraction.get("status") == "deferred_or_partial":
            state = "partial"
        elif extraction.get("status") in {"processed", "no_eligible_positive"}:
            state = "completed"
        else:
            state = "failed"
        row["queue_state"] = state
        articles.append(row)
    issues = Counter()
    for record in records:
        for issue in record["assessment"].get("execution", {}).get("issues", []):
            issues[issue.get("code", "unspecified") if isinstance(issue, dict) else str(issue)] += 1
    selected = sorted(records, key=lambda r: identity([folder.name, r["group_id"]]))[:3]
    # This is a fixed review sample, not generated labels or an admission gate.
    sample = {"source_run": folder.name, "status": "unreviewed", "criteria":
              ["主癌种与具体药物正确", "关系及影响结论的必要条件正确", "文章和段落引用支持知识"],
              "records": selected}
    atomic_json(folder / "evaluation-sample.json", sample)
    summary = {"articles": articles, "knowledge_records": len(records),
               "structurally_eligible": sum(r["assessment"].get("eligibility", {}).get("status") == "eligible" for r in records),
               "offline_ready_count": result.get("offline_ready_count", 0), "reviewed_records": 0,
               "quality_status": "unreviewed", "issues": dict(issues), "target_written": False}
    from apps.curation.knowledge_target_readiness import readiness_summary

    summary["target_readiness"] = readiness_summary(records)
    atomic_json(folder / "processing-report.json", summary)
    text = (f"# {folder.name}\n\n自动处理 {len(articles)} 篇，保存 {len(records)} 条提取记录，"
            f"结构合格 {summary['structurally_eligible']} 条，目标离线结构通过 {summary['offline_ready_count']} 条。\n\n"
            "以上不是医学正确率。固定抽查样本见 evaluation-sample.json；全文及文章/段落出处沿用原记录。\n\n"
            f"执行问题统计：{json.dumps(dict(issues), ensure_ascii=False)}。\n\n"
            "先处理反复出现、阻断产出的共性问题；单个未定实体不阻断其他知识，不自动改变提示词或审核答案。\n")
    (folder / "experience.md").write_text(text)
    return summary
