import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import IssueCreate, IssueUpdate


class IssueStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    observation TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    run_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list(self, limit: int | None = 200):
        with self.connect() as connection:
            query = "SELECT * FROM issues ORDER BY created_at DESC, id DESC"
            rows = connection.execute(
                query if limit is None else query + " LIMIT ?",
                () if limit is None else (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def create(self, item: IssueCreate):
        now = datetime.now(UTC).isoformat()
        result = {
            "id": str(uuid4()),
            **item.model_dump(mode="json"),
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO issues VALUES (:id, :title, :category, :observation, "
                ":expected, :lesson, :run_id, :status, :created_at, :updated_at)",
                result,
            )
        return result

    def update(self, issue_id: str, item: IssueUpdate):
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE issues SET status=?, lesson=?, updated_at=? WHERE id=?",
                (item.status, item.lesson, datetime.now(UTC).isoformat(), issue_id),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
            return dict(row)
