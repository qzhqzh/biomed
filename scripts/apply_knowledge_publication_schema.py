"""Apply only 0011; check affected small tables, avoiding a global SQLite scan."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.apply_knowledge_schema import schema_snapshot

TARGET = ("curation", "0011_knowledge_publication")
NEW_MODELS = {"knowledgereview", "knowledgesearchterm"}
NEW_TABLES = {f"curation_{name}" for name in NEW_MODELS}
CANDIDATE = "curation_knowledgecandidate"
TABLES = NEW_TABLES | {CANDIDATE, "curation_knowledgeanchor"}
FIELDS = {
    "knowledgecandidate": {"ai_status", "human_status", "publication_status", "published_at", "current_review"},
    "knowledgesearchterm": {"candidate", "entity", "review"},
}


def verify_plan(plan):
    from django.db.migrations.operations import fields, models
    if len(plan) != 1 or (plan[0][0].app_label, plan[0][0].name) != TARGET or plan[0][1]:
        raise ValueError("Require only forward 0011")
    migration = plan[0][0]
    if migration.atomic is not True:
        raise ValueError("Require atomic migration")
    created, added, constraints, indexes = set(), set(), set(), set()
    for op in migration.operations:
        if type(op) is models.CreateModel:
            name = op.name.lower()
            if name not in NEW_MODELS or name in created or op.options.get("db_table", f"curation_{name}") != f"curation_{name}":
                raise ValueError("Unexpected new model")
            created.add(name)
        elif type(op) is fields.AddField:
            pair = (op.model_name.lower(), op.name)
            if op.name not in FIELDS.get(pair[0], set()) or pair in added:
                raise ValueError("Unexpected added field")
            added.add(pair)
        elif type(op) is models.AddConstraint:
            pair = (op.model_name.lower(), op.constraint.name)
            if pair in constraints:
                raise ValueError("Duplicate constraint")
            constraints.add(pair)
        elif type(op) is models.AddIndex:
            pair = (op.model_name.lower(), op.index.name)
            if pair in indexes:
                raise ValueError("Duplicate index")
            indexes.add(pair)
        else:
            raise ValueError("Refusing unrelated or destructive operation")
    if (created != NEW_MODELS or added != {(m, f) for m, fs in FIELDS.items() for f in fs}
        or constraints != {("knowledgecandidate", "knowledge_published_requires_ai"),
                           ("knowledgesearchterm", "unique_review_entity_term")}
        or indexes != {("knowledgesearchterm", "knowledge_term_lookup")}):
        raise ValueError("Unexpected publication schema scope")


def apply():
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    if connection.vendor != "sqlite":
        raise ValueError("SQLite-only operational helper")
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan([TARGET])
    before = schema_snapshot(connection)
    original = connection.check_constraints
    if not plan:
        if not NEW_TABLES <= {name for kind, name in before if kind == "table"}:
            raise ValueError("Applied migration has missing tables")
        original(sorted(TABLES))
        return {"status": "already_applied", "target": TARGET}
    verify_plan(plan)
    if NEW_TABLES & {name for _, name in before}:
        raise ValueError("New tables exist without migration record")
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM "{CANDIDATE}" ORDER BY id')
        old_columns = [column[0] for column in cursor.description]
        old_rows = cursor.fetchall()

    def check_scoped(table_names=None):
        names = TABLES if table_names is None else set(table_names)
        if not names <= TABLES:
            raise ValueError("Refusing unrelated FK check")
        return original(sorted(names))

    connection.check_constraints = check_scoped
    try:
        executor.migrate([TARGET], plan=plan)
    except BaseException:
        connection.close()  # Roll back even if schema-editor exit check failed.
        raise
    finally:
        connection.check_constraints = original
    after = schema_snapshot(connection)
    if any(key not in after or after[key] != value for key, value in before.items() if value[0] != CANDIDATE):
        raise ValueError("Unrelated schema changed")
    with connection.cursor() as cursor:
        columns = ",".join(connection.ops.quote_name(c) for c in old_columns)
        cursor.execute(f'SELECT {columns} FROM "{CANDIDATE}" ORDER BY id')
        if cursor.fetchall() != old_rows:
            raise ValueError("Original candidate data changed")
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise ValueError("Foreign keys disabled")
    original(sorted(TABLES))
    return {"status": "applied", "target": TARGET, "original_candidate_rows": len(old_rows),
            "original_candidate_data_unchanged": True, "unrelated_schema_unchanged": True,
            "constraint_checked_tables": sorted(TABLES)}


if __name__ == "__main__":
    import json
    print(json.dumps(apply()), flush=True)
