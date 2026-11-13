"""Material-list index safety and query plans, on temporary SQLite only."""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.db import migrations, models

from scripts.apply_collection_indexes import MATERIAL_TARGET, verify_plan

PROJECT = Path(__file__).resolve().parents[1]


class MaterialListIndexTests(unittest.TestCase):
    def migration(self):
        module = importlib.import_module(f"apps.curation.migrations.{MATERIAL_TARGET[1]}")
        return module.Migration(MATERIAL_TARGET[1], MATERIAL_TARGET[0])

    def test_material_target_requires_only_its_exact_forward_index(self):
        migration = self.migration()
        verify_plan([(migration, False)], MATERIAL_TARGET)
        with self.assertRaises(ValueError):
            verify_plan([(migration, False)])
        for plan in ([], [(migration, True)], [(migration, False)] * 2):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                verify_plan(plan, MATERIAL_TARGET)
        for operation in (
            migrations.RunSQL("SELECT 1"),
            migrations.AddIndex("material", models.Index(fields=["created_at", "kind", "id"], name="material_list_created")),
            migrations.AddIndex("material", models.Index(fields=["kind", "-created_at", "id"], name="material_list_created")),
        ):
            migration = self.migration()
            migration.operations = [operation]
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                verify_plan([(migration, False)], MATERIAL_TARGET)

    def test_real_material_migration_rolls_back_then_preserves_rows_and_plans(self):
        with tempfile.TemporaryDirectory(prefix="biomed-material-list-index-") as directory:
            env = os.environ.copy()
            env.update(DATABASE_PATH=str(Path(directory) / "test.sqlite3"), DJANGO_DEBUG="1")
            env.pop("BIOMED_STORAGE_MARKER", None)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, *args], cwd=PROJECT, env=env,
                    capture_output=True, text=True, check=False, timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("manage.py", "migrate", "curation", "0013", "--noinput", "--skip-checks")
            output = run("-c", r'''
import json, os, sqlite3
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from unittest.mock import patch
from django.db import connection, connections
from apps.curation.models import Material
from scripts.apply_collection_indexes import apply, schema_snapshot, MATERIAL_TARGET

Material.objects.bulk_create([
    Material(source_key=f"material-index:{i}", title=f"Unchanged title {i}", kind="guideline" if i % 10 == 0 else "paper")
    for i in range(2000)
])
before = schema_snapshot(connection)
old_rows = list(Material.objects.order_by("id").values())
original = connection.check_constraints
with patch("scripts.apply_collection_indexes.verify_indexes", side_effect=RuntimeError("injected material schema failure")):
    try:
        apply(target="material-list")
        raise AssertionError("schema failure was ignored")
    except RuntimeError as error:
        assert str(error) == "injected material schema failure", error
assert connection.check_constraints == original
with sqlite3.connect(os.environ["DATABASE_PATH"]) as check:
    after = {(a,b):(c,d) for a,b,c,d in check.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}
    assert after == before, "DDL did not roll back"
    assert check.execute("SELECT count(*) FROM django_migrations WHERE app=? AND name=?", MATERIAL_TARGET).fetchone()[0] == 0
del connections["default"]
connection.ensure_connection()
trace, events = [], []
connection.connection.set_trace_callback(trace.append)
with connection.cursor() as cursor:
    cursor.execute("PRAGMA journal_mode")
    journal_before = cursor.fetchone()[0]
first = apply(target="material-list", progress=events.append)
assert first["status"] == "applied"
assert [event["index"] for event in events if event["event"] == "index_complete"] == ["material_list_created"]
assert list(Material.objects.order_by("id").values()) == old_rows
assert all(schema_snapshot(connection).get(key) == value for key, value in before.items())
second = apply(target="material-list")
assert second["status"] == "already_applied"
# The default 0013 helper remains idempotent even after its successor was applied.
assert apply()["status"] == "already_applied"
assert not any("foreign_key_check" in sql.lower() for sql in trace), trace
plans = {}
for kind in (None, "paper", "guideline", "other"):
    rows = Material.objects.order_by("-created_at")
    if kind:
        rows = rows.filter(kind=kind)
    plans[kind or "all"] = rows.values_list("id", flat=True)[:50].explain().splitlines()
    assert any("COVERING INDEX material_list_created" in row for row in plans[kind or "all"]), plans
    assert not any("TEMP B-TREE" in row for row in plans[kind or "all"]), plans
with connection.cursor() as cursor:
    cursor.execute("EXPLAIN QUERY PLAN SELECT COUNT(id), COUNT(id) FILTER (WHERE kind='paper'), COUNT(id) FILTER (WHERE kind='guideline'), COUNT(id) FILTER (WHERE kind='other') FROM curation_material")
    plans["conditional_counts"] = [row[3] for row in cursor.fetchall()]
    assert any("COVERING INDEX material_list_created" in row for row in plans["conditional_counts"]), plans
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0] == journal_before
    cursor.execute("DROP INDEX material_list_created")
    cursor.execute("CREATE INDEX material_list_created ON curation_material(created_at,kind,id)")
try:
    apply(target="material-list")
    raise AssertionError("wrong direction accepted")
except ValueError as error:
    assert "ordering" in str(error), error
print(json.dumps({"first":first,"second":second,"plans":plans,"rollback_verified":True,"rows_unchanged":True}))
''')
            result = json.loads(output)
            self.assertTrue(result["rollback_verified"])
            self.assertTrue(result["rows_unchanged"])


if __name__ == "__main__":
    unittest.main()
