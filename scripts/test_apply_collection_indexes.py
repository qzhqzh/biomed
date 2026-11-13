"""Migration safety and query-plan checks, using temporary SQLite only."""

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.db import migrations, models

from scripts.apply_collection_indexes import TARGET, verify_plan

PROJECT = Path(__file__).resolve().parents[1]


class CollectionIndexTests(unittest.TestCase):
    def migration(self):
        module = importlib.import_module(f"apps.curation.migrations.{TARGET[1]}")
        return module.Migration(TARGET[1], TARGET[0])

    def test_only_exact_atomic_forward_plan_is_allowed(self):
        migration = self.migration()
        verify_plan([(migration, False)])
        for plan in ([], [(migration, True)], [(migration, False)] * 2):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                verify_plan(plan)
        migration.atomic = False
        with self.assertRaises(ValueError):
            verify_plan([(migration, False)])

    def test_extra_missing_duplicate_or_modified_operations_are_rejected(self):
        original = self.migration().operations
        alternatives = [
            original[:-1],
            [*original, copy.deepcopy(original[0])],
            [*original, migrations.RunSQL("SELECT 1")],
            [*original, migrations.RemoveIndex("collectionentry", "collect_entry_fulltext")],
        ]
        for changed_index in (
            models.Index(fields=["id"], name="collect_entry_fulltext"),
            models.Index(fields=["collection", "fulltext_state", "id"], name="collect_entry_fulltext", condition=models.Q(fulltext_state="pending")),
        ):
            alternatives.append([
                migrations.AddIndex("collectionentry", changed_index), *original[1:]
            ])
        for operations in alternatives:
            migration = self.migration()
            migration.operations = operations
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                verify_plan([(migration, False)])

    def test_real_migration_rollback_idempotency_and_covering_query_plans(self):
        with tempfile.TemporaryDirectory(prefix="biomed-collection-indexes-") as directory:
            env = os.environ.copy()
            env.update(DATABASE_PATH=str(Path(directory) / "test.sqlite3"), DJANGO_DEBUG="1")
            env.pop("BIOMED_STORAGE_MARKER", None)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, *args], cwd=PROJECT, env=env,
                    check=False, text=True, capture_output=True, timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("manage.py", "migrate", "curation", "0012", "--noinput", "--skip-checks")
            output = run("-c", r'''
import json, os, sqlite3
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from unittest.mock import patch
from django.db import connection, connections
from django.db.models import Count
from apps.curation.models import LiteratureCollection, CollectionEntry, CollectionFetch, Material, MaterialVersion
from scripts.apply_collection_indexes import apply, schema_snapshot, INDEXES, TARGET

run = LiteratureCollection.objects.create(start_date="2021-01-01", end_date="2026-01-01", query="test", taxonomy={}, limits={})
material = Material.objects.create(source_key="index-test:sentinel", title="Unchanged source")
version = MaterialVersion.objects.create(material=material, sha256="a" * 64, media_type="application/xml", filename="source.xml", rights="test-only")
CollectionEntry.objects.bulk_create([
    CollectionEntry(collection=run, pmid=str(i), metadata_version=version, fulltext_state="parsed" if i % 2 else "pending", identifiers={"pmc":f"PMC{i}"})
    for i in range(1, 2001)
])
CollectionFetch.objects.bulk_create([
    CollectionFetch(collection=run, url="https://example.test/" + str(i), charged_bytes=i)
    for i in range(1, 101)
])
before = schema_snapshot(connection)
old_entries = list(CollectionEntry.objects.order_by("id").values())
old_fetches = list(CollectionFetch.objects.order_by("id").values())
original_check = connection.check_constraints

def verify_rollback():
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as check:
        after = {(a,b):(c,d) for a,b,c,d in check.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}
        assert after == before, "Index DDL failed to roll back"
        assert check.execute("SELECT count(*) FROM django_migrations WHERE app=? AND name=?", TARGET).fetchone()[0] == 0
    assert connection.check_constraints.__func__ is original_check.__func__
    # An operational failure exits its dedicated CLI process. Recreate that
    # connection wrapper for the next isolated invocation in this test process.
    del connections["default"]

with patch("scripts.apply_collection_indexes.verify_indexes", side_effect=RuntimeError("injected schema failure")):
    try:
        apply()
        raise AssertionError("schema failure was ignored")
    except RuntimeError as error:
        assert str(error) == "injected schema failure"
verify_rollback()

times = iter(range(1000000))
with patch("scripts.apply_collection_indexes.time.monotonic", side_effect=lambda: next(times)):
    try:
        apply(max_seconds=1)
        raise AssertionError("deadline was ignored")
    except RuntimeError as error:
        assert "time limit" in str(error), error
verify_rollback()

connection.ensure_connection()
original_check = connection.check_constraints
trace, events = [], []
connection.connection.set_trace_callback(trace.append)
with connection.cursor() as cursor:
    cursor.execute("PRAGMA journal_mode")
    journal_before = cursor.fetchone()[0]
    cursor.execute("PRAGMA busy_timeout")
    timeout_before = cursor.fetchone()[0]
first = apply(progress=events.append)
assert first["status"] == "applied"
assert connection.check_constraints == original_check
assert [event["index"] for event in events if event["event"] == "index_complete"] == list(INDEXES)
assert list(CollectionEntry.objects.order_by("id").values()) == old_entries
assert list(CollectionFetch.objects.order_by("id").values()) == old_fetches
assert not run.entries.filter(metadata_version__isnull=True).exists()
second = apply()
assert second["status"] == "already_applied"
assert not any("foreign_key_check" in sql.lower() for sql in trace), trace
with connection.cursor() as cursor:
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0] == journal_before
    cursor.execute("PRAGMA busy_timeout")
    assert cursor.fetchone()[0] == timeout_before
    plans = {}
    for label, (name, sql) in {
        "fulltext_attempted": ("collect_entry_fulltext", "SELECT COUNT(*) FROM curation_collectionentry WHERE collection_id=%s AND fulltext_state IN ('parsed','reused','unavailable')"),
        "metadata_pending": ("collect_entry_fulltext", "SELECT 1 FROM curation_collectionentry WHERE collection_id=%s AND metadata_version_id IS NULL LIMIT 1"),
        "budget": ("collect_fetch_budget", "SELECT SUM(charged_bytes) FROM curation_collectionfetch WHERE collection_id=%s"),
        "fetch_metrics": ("collect_fetch_budget", "SELECT COUNT(id), SUM(wire_bytes), SUM(decoded_bytes), SUM(charged_bytes), SUM(elapsed_seconds), COUNT(id) FILTER (WHERE state='failed'), COUNT(id) FILTER (WHERE state='running') FROM curation_collectionfetch WHERE collection_id=%s"),
    }.items():
        cursor.execute("EXPLAIN QUERY PLAN " + sql, [run.pk.hex])
        plans[label] = [row[3] for row in cursor.fetchall()]
        assert any(f"COVERING INDEX {name}" in row for row in plans[label]), plans
    summary = run.entries.order_by().values("fulltext_state", "reused").annotate(total=Count("id"), saved=Count("metadata_version_id"))
    plans["entry_summary"] = summary.explain().splitlines()
    assert any("COVERING INDEX collect_entry_fulltext" in row for row in plans["entry_summary"]), plans
    assert not any("SCAN curation_collectionentry" in row for row in plans["entry_summary"]), plans
    pending = run.entries.filter(fulltext_state="pending", identifiers__pmc__startswith="PMC").order_by("id")[:1]
    plans["pending_first"] = pending.explain().splitlines()
    assert any("INDEX collect_entry_fulltext" in row for row in plans["pending_first"]), plans
    assert not any("TEMP B-TREE" in row for row in plans["pending_first"]), plans
    plans["global_last_request"] = CollectionFetch.objects.order_by("-started_at")[:1].explain().splitlines()
    assert any("INDEX collect_fetch_started" in row for row in plans["global_last_request"]), plans
    assert not any("TEMP B-TREE" in row for row in plans["global_last_request"]), plans
    cursor.execute("DROP INDEX collect_fetch_started")
try:
    apply()
    raise AssertionError("missing applied index was accepted")
except ValueError as error:
    assert "Missing or unexpected" in str(error), error
print(json.dumps({"first":first,"second":second,"plans":plans,"rows_unchanged":True,"rollback_verified":True}))
''')
            result = json.loads(output)
            self.assertTrue(result["rows_unchanged"])
            self.assertTrue(result["rollback_verified"])


if __name__ == "__main__":
    unittest.main()
