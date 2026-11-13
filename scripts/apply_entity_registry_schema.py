"""Apply ONLY the additive entity-registry migration, checking its five new tables.

Operational alternative for a large existing SQLite database: Django otherwise
checks every old foreign key while holding the migration write transaction.
This does not fake a migration or change global constraint-checking behavior.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET = ("curation", "0012_entity_registry")
MODELS = {
    "canonicalentity",
    "entityresolutionrule",
    "entityregistrymention",
    "entityregistrydecision",
    "entityregistryimport",
}
TABLES = {f"curation_{name}" for name in MODELS}


def verify_plan(plan):
    from django.db.migrations.operations.models import (
        AddConstraint,
        AddIndex,
        CreateModel,
    )

    if (
        len(plan) != 1
        or (plan[0][0].app_label, plan[0][0].name) != TARGET
        or plan[0][1]
    ):
        raise ValueError("Refusing any migration plan other than forward 0012")
    if plan[0][0].atomic is not True:
        raise ValueError("The additive migration must remain atomic")
    created = set()
    for operation in plan[0][0].operations:
        if type(operation) is CreateModel:
            name = operation.name.lower()
            table = operation.options.get("db_table", f"curation_{name}")
            if name not in MODELS or name in created or table != f"curation_{name}":
                raise ValueError("Unexpected table in additive migration")
            created.add(name)
        elif (
            type(operation) in {AddConstraint, AddIndex}
            and operation.model_name.lower() in MODELS
        ):
            continue
        else:
            raise ValueError("Refusing operation that could change existing tables")
    if created != MODELS:
        raise ValueError("Expected exactly five new registry models")


def schema_snapshot(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        return {(row[0], row[1]): (row[2], row[3]) for row in cursor.fetchall()}


def verify_new_tables(connection):
    snapshot = schema_snapshot(connection)
    if not TABLES <= {name for kind, name in snapshot if kind == "table"}:
        raise ValueError("Missing new tables")
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise ValueError("Foreign key enforcement is not enabled")


def apply():
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    if connection.vendor != "sqlite":
        raise ValueError("This operational helper is only for SQLite")
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan([TARGET])
    if not plan:
        verify_new_tables(connection)
        connection.check_constraints(sorted(TABLES))
        return {
            "status": "already_applied",
            "target": TARGET,
            "constraint_checked_tables": sorted(TABLES),
        }
    verify_plan(plan)
    before = schema_snapshot(connection)
    if TABLES & {name for _, name in before}:
        raise ValueError(
            "New tables already exist without the migration; investigate first"
        )
    original = connection.check_constraints

    def check_new_tables(table_names=None):
        names = TABLES if table_names is None else set(table_names)
        if not names <= TABLES:
            raise ValueError("Refusing constraint-check scope outside the new tables")
        return original(sorted(names))

    connection.check_constraints = check_new_tables
    try:
        executor.migrate([TARGET], plan=plan)
    except BaseException:
        # SQLite's schema-editor constraint check precedes its atomic cleanup.
        # Close this dedicated CLI connection to roll back on that failure path.
        connection.close()
        raise
    finally:
        connection.check_constraints = original
    after = schema_snapshot(connection)
    if any(key not in after or after[key] != sql for key, sql in before.items()):
        raise ValueError("Existing schema changed unexpectedly; investigate")
    verify_new_tables(connection)
    original(sorted(TABLES))
    return {
        "status": "applied",
        "target": TARGET,
        "constraint_checked_tables": sorted(TABLES),
        "existing_schema_unchanged": True,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(apply()), flush=True)
