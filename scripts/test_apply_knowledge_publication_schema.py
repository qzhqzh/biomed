"""Publication migration safety checks; all database writes use temporary SQLite."""

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from django.db import migrations, models

from scripts.apply_knowledge_publication_schema import TARGET, verify_plan

PROJECT = Path(__file__).resolve().parents[1]


class KnowledgePublicationMigrationTests(unittest.TestCase):
    def migration(self):
        with patch.dict(os.environ, {
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DJANGO_DEBUG": "1",
            "DATABASE_PATH": ":memory:",
        }):
            module = importlib.import_module(
                "apps.curation.migrations.0011_knowledge_publication"
            )
        return module.Migration(TARGET[1], TARGET[0])

    def test_only_exact_atomic_forward_plan_is_accepted(self):
        migration = self.migration()
        verify_plan([(migration, False)])
        for plan in ([], [(migration, True)], [(migration, False)] * 2):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                verify_plan(plan)
        for attribute, value in (
            ("name", "0010_knowledge_candidates"),
            ("app_label", "annotations"),
            ("atomic", False),
        ):
            migration = self.migration()
            setattr(migration, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(ValueError):
                verify_plan([(migration, False)])

    def test_unrelated_or_destructive_operations_are_rejected(self):
        for operation in (
            migrations.CreateModel("Material", []),
            migrations.AddField("material", "test", models.TextField()),
            migrations.AlterField("knowledgecandidate", "fact", models.TextField()),
            migrations.RemoveField("knowledgecandidate", "fact"),
            migrations.DeleteModel("Material"),
            migrations.RunSQL("SELECT 1"),
        ):
            migration = self.migration()
            migration.operations = [*migration.operations, operation]
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                verify_plan([(migration, False)])

    def test_missing_and_duplicate_operations_are_rejected(self):
        original = self.migration().operations
        for index, operation in enumerate(original):
            for mode in ("missing", "duplicate"):
                migration = self.migration()
                migration.operations = (
                    original[:index] + original[index + 1 :]
                    if mode == "missing"
                    else [*original, copy.deepcopy(operation)]
                )
                with self.subTest(mode=mode, operation=operation), self.assertRaises(
                    ValueError
                ):
                    verify_plan([(migration, False)])

    def test_redirected_new_table_is_rejected(self):
        migration = self.migration()
        migration.operations = copy.deepcopy(migration.operations)
        create = next(
            op for op in migration.operations if isinstance(op, migrations.CreateModel)
        )
        create.options["db_table"] = "curation_material"
        with self.assertRaises(ValueError):
            verify_plan([(migration, False)])

    def test_real_0010_to_0011_is_scoped_preserves_rows_and_rolls_back(self):
        with tempfile.TemporaryDirectory(
            prefix="biomed-knowledge-publication-migration-"
        ) as directory:
            env = os.environ.copy()
            env.update(
                DATABASE_PATH=str(Path(directory) / "test.sqlite3"),
                CURATION_STORAGE_DIR=str(Path(directory) / "library"),
                DJANGO_DEBUG="1",
            )
            env.pop("BIOMED_STORAGE_MARKER", None)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, *args],
                    cwd=PROJECT,
                    env=env,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run(
                "manage.py", "migrate", "curation", "0010", "--noinput", "--skip-checks"
            )
            # Seed using the historical model state, never today's new columns.
            run(
                "-c",
                r'''
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
apps = MigrationExecutor(connection).loader.project_state([
    ("curation", "0010_knowledge_candidates")
]).apps
Material = apps.get_model("curation", "Material")
Version = apps.get_model("curation", "MaterialVersion")
Passage = apps.get_model("curation", "Passage")
Candidate = apps.get_model("curation", "KnowledgeCandidate")
Entity = apps.get_model("curation", "KnowledgeEntity")
Anchor = apps.get_model("curation", "KnowledgeAnchor")
material = Material.objects.create(source_key="migration:sentinel", title="Unchanged source")
version = Version.objects.create(material=material, sha256="a" * 64,
    media_type="application/xml", filename="source.xml", rights="test-only")
passage = Passage.objects.create(version=version, parser_version="test-v1", ordinal=1,
    locator="Results / paragraph 1", text="Original English evidence, unchanged.")
entity = Entity.objects.create(scope_version=version, identity_key="b" * 64,
    kind="drug", label="durvalumab", attributes={"exact": "durvalumab"})
for number in range(2):
    candidate = Candidate.objects.create(source_run="migration-test", group_id=f"K0{number + 1}",
        version=version, content_sha256=str(number) * 64, import_sha256="c" * 64,
        fact={"treatment": "durvalumab", "population": "胆道癌", "value": [1, None, False]},
        assessment={"existing_reference": number == 0}, provenance={"version": "preserve"})
    Anchor.objects.create(candidate=candidate, entity=entity, passage=passage,
        content_sha256="d" * 64, entity_ref="drug:1", purpose="evidence",
        association="model_selected", roles=["drug"], start=0, end=8, quote="Original")
with connection.cursor() as cursor:
    cursor.execute("CREATE TABLE unrelated_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    cursor.execute("INSERT INTO unrelated_sentinel VALUES (1, 'keep')")
''',
            )
            # An error from schema-editor exit checks must roll back every DDL
            # operation, old candidate rows, and the migration recorder entry.
            run(
                "-c",
                r'''
import os, sqlite3
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from unittest.mock import patch
from scripts.apply_knowledge_publication_schema import apply, CANDIDATE, TARGET
from scripts.apply_knowledge_schema import schema_snapshot
before = schema_snapshot(connection)
with sqlite3.connect(os.environ["DATABASE_PATH"]) as check:
    old_rows = check.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id').fetchall()
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
    assert check.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id').fetchall() == old_rows
    assert check.execute("SELECT count(*) FROM django_migrations WHERE app=? AND name=?", TARGET).fetchone()[0] == 0
''',
            )
            result = json.loads(
                run(
                    "-c",
                    r'''
import os, json, re
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from scripts.apply_knowledge_publication_schema import apply, TABLES, CANDIDATE, TARGET
from scripts.apply_knowledge_schema import schema_snapshot
connection.ensure_connection()
sql = []
connection.connection.set_trace_callback(sql.append)
original = connection.check_constraints
before = schema_snapshot(connection)
with connection.cursor() as cursor:
    cursor.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id')
    old_columns = [column[0] for column in cursor.description]
    old_rows = cursor.fetchall()
    cursor.execute('SELECT * FROM curation_knowledgeanchor ORDER BY id')
    old_anchors = cursor.fetchall()
first = apply()
assert first["status"] == "applied"
assert first["original_candidate_rows"] == 2
assert connection.check_constraints == original
after = schema_snapshot(connection)
assert all(key in after and after[key] == value for key,value in before.items() if value[0] != CANDIDATE)
with connection.cursor() as cursor:
    columns = ",".join(connection.ops.quote_name(c) for c in old_columns)
    cursor.execute(f'SELECT {columns} FROM "{CANDIDATE}" ORDER BY id')
    assert cursor.fetchall() == old_rows
    cursor.execute('SELECT * FROM curation_knowledgeanchor ORDER BY id')
    assert cursor.fetchall() == old_anchors
    cursor.execute('SELECT value FROM unrelated_sentinel WHERE id=1')
    assert cursor.fetchone() == ("keep",)
    cursor.execute(f'SELECT ai_status,human_status,publication_status,published_at,current_review_id FROM "{CANDIDATE}"')
    assert cursor.fetchall() == [("pending", "unchecked", "draft", None, None)] * 2
    cursor.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT count(*) FROM django_migrations WHERE app=%s AND name=%s", TARGET)
    assert cursor.fetchone()[0] == 1
    cursor.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id')
    first_rows = cursor.fetchall()
second = apply()
assert second["status"] == "already_applied"
assert schema_snapshot(connection) == after, "idempotent run changed schema"
with connection.cursor() as cursor:
    cursor.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id')
    assert cursor.fetchall() == first_rows, "idempotent run changed rows"
checks = [statement for statement in sql if statement.lower().startswith("pragma foreign_key_check")]
checked = set()
for statement in checks:
    match = re.fullmatch(r'PRAGMA foreign_key_check\("([^\"]+)"\)', statement, re.IGNORECASE)
    assert match, f"unscoped or unexpected FK check: {statement}"
    checked.add(match.group(1))
assert checked == TABLES, (checked, TABLES)
assert connection.check_constraints == original
with connection.cursor() as cursor:
    cursor.execute("DROP TABLE curation_knowledgesearchterm")
try:
    apply()
    raise AssertionError("missing applied table was accepted")
except ValueError as error:
    assert str(error) == "Applied migration has missing tables"
print(json.dumps({"first": first, "second": second, "checked_tables": sorted(checked),
    "original_rows_unchanged": True, "anchors_unchanged": True, "idempotent": True}))
''',
                )
            )
            self.assertTrue(result["first"]["original_candidate_data_unchanged"])
            self.assertTrue(result["first"]["unrelated_schema_unchanged"])
            self.assertTrue(result["anchors_unchanged"])
            self.assertTrue(result["idempotent"])


if __name__ == "__main__":
    unittest.main()
