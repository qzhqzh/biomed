"""Operational migration guard tests; all writes use a temporary SQLite database."""

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.db import migrations

from scripts.apply_knowledge_schema import TARGET, verify_plan

PROJECT = Path(__file__).resolve().parents[1]


class KnowledgeMigrationPlanTests(unittest.TestCase):
    def migration(self):
        module = importlib.import_module(
            "apps.curation.migrations.0010_knowledge_candidates"
        )
        return module.Migration(TARGET[1], TARGET[0])

    def test_exact_forward_plan_is_required(self):
        migration = self.migration()
        verify_plan([(migration, False)])
        for plan in ([], [(migration, True)], [(migration, False), (migration, False)]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                verify_plan(plan)

    def test_old_table_sql_duplicate_creation_and_nonatomic_are_rejected(self):
        for operation in (
            migrations.CreateModel("Material", []),
            migrations.RunSQL("SELECT 1"),
            copy.deepcopy(self.migration().operations[0]),
        ):
            migration = self.migration()
            migration.operations = [*migration.operations, operation]
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                verify_plan([(migration, False)])
        migration = self.migration()
        migration.atomic = False
        with self.assertRaises(ValueError):
            verify_plan([(migration, False)])

    def test_real_0009_to_0010_is_scoped_atomic_and_repeatable(self):
        with tempfile.TemporaryDirectory(
            prefix="biomed-knowledge-migration-"
        ) as directory:
            env = os.environ.copy()
            env.update(
                DATABASE_PATH=str(Path(directory) / "test.sqlite3"), DJANGO_DEBUG="1"
            )
            env.pop("BIOMED_STORAGE_MARKER", None)

            def run(*args):
                return subprocess.run(
                    [sys.executable, *args],
                    cwd=PROJECT,
                    env=env,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=120,
                ).stdout

            run(
                "manage.py", "migrate", "curation", "0009", "--noinput", "--skip-checks"
            )
            # Failure during Django's exit-time FK check must roll back the DDL.
            run(
                "-c",
                r"""
import os, sqlite3
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from unittest.mock import patch
from apps.curation.models import Material
from scripts.apply_knowledge_schema import apply, schema_snapshot
Material.objects.create(source_key="migration:sentinel", title="unchanged")
before = schema_snapshot(connection)
original = connection.check_constraints
with patch.object(connection, "check_constraints", side_effect=RuntimeError("test FK failure")):
    try:
        apply()
        raise AssertionError("expected migration failure")
    except RuntimeError as error:
        assert str(error) == "test FK failure"
assert connection.check_constraints == original
with sqlite3.connect(os.environ["DATABASE_PATH"]) as check:
    after = {(a,b):(c,d) for a,b,c,d in check.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}
    assert after == before, "DDL did not roll back after constraint failure"
    assert check.execute("SELECT count(*) FROM django_migrations WHERE app='curation' AND name='0010_knowledge_candidates'").fetchone()[0] == 0
""",
            )
            result = json.loads(
                run(
                    "-c",
                    r"""
import os, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from apps.curation.models import Material
from scripts.apply_knowledge_schema import apply, TABLES, schema_snapshot
connection.ensure_connection()
sql = []
connection.connection.set_trace_callback(sql.append)
original = connection.check_constraints
before = schema_snapshot(connection)
first = apply()
assert first["status"] == "applied"
assert connection.check_constraints == original
after = schema_snapshot(connection)
assert all(key in after and after[key] == value for key,value in before.items())
assert Material.objects.get(source_key="migration:sentinel").title == "unchanged"
second = apply()
assert second["status"] == "already_applied"
checks = [statement for statement in sql if statement.lower().startswith("pragma foreign_key_check")]
assert checks and all("(" in statement for statement in checks), checks
assert all(any(table in statement for table in TABLES) for statement in checks), checks
with connection.cursor() as cursor:
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT count(*) FROM django_migrations WHERE app='curation' AND name='0010_knowledge_candidates'")
    assert cursor.fetchone()[0] == 1
    cursor.execute("DROP TABLE curation_knowledgeanchor")
try:
    apply()
    raise AssertionError("missing applied table was accepted")
except ValueError as error:
    assert str(error) == "Missing new tables"
print(json.dumps({"first":first,"second":second,"scoped_fk_checks":checks,"sentinel_unchanged":True}))
""",
                )
            )
            self.assertTrue(result["sentinel_unchanged"])
            self.assertTrue(result["first"]["existing_schema_unchanged"])


if __name__ == "__main__":
    unittest.main()
