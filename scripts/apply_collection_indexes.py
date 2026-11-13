"""Apply one explicitly approved index-only migration with bounded SQLite execution.

No rows, tables, constraints, journal mode or global FK settings are changed.
The index-only migration needs schema verification, not a full FK data scan.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET = ("curation", "0013_collection_hotpath_indexes")
INDEXES = {
    "collect_entry_fulltext": ("collectionentry", ("collection", "fulltext_state", "id", "reused", "metadata_version")),
    "collect_fetch_budget": ("collectionfetch", ("collection", "charged_bytes", "wire_bytes", "decoded_bytes", "elapsed_seconds", "state")),
    "collect_fetch_started": ("collectionfetch", ("started_at",)),
}
MATERIAL_TARGET = ("curation", "0014_material_list_index")
MATERIAL_INDEXES = {
    "material_list_created": ("material", ("-created_at", "kind", "id")),
}
TARGET_NAMES = {"collection": TARGET, "material-list": MATERIAL_TARGET}


def approved_indexes(target):
    if target == TARGET:
        return INDEXES
    if target == MATERIAL_TARGET:
        return MATERIAL_INDEXES
    raise ValueError("Unknown index migration target")


def verify_plan(plan, target=TARGET):
    from django.db import migrations, models

    indexes = approved_indexes(target)
    if (
        len(plan) != 1
        or (plan[0][0].app_label, plan[0][0].name) != target
        or plan[0][1]
        or plan[0][0].atomic is not True
    ):
        raise ValueError(f"Require only the atomic forward {target[1]} migration")
    found = set()
    for operation in plan[0][0].operations:
        if type(operation) is not migrations.AddIndex:
            raise ValueError("Only the target's approved AddIndex operations are allowed")
        index = operation.index
        expected = indexes.get(index.name)
        if (
            not expected
            or index.name in found
            or operation.model_name.lower() != expected[0]
            or type(index) is not models.Index
            or index.deconstruct()
            != models.Index(fields=list(expected[1]), name=index.name).deconstruct()
        ):
            raise ValueError("Unexpected index definition")
        found.add(index.name)
    if found != set(indexes):
        raise ValueError("Require exactly all approved indexes for this target")


def schema_snapshot(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        return {(a, b): (c, d) for a, b, c, d in cursor.fetchall()}


def verify_indexes(connection, target=TARGET):
    indexes = approved_indexes(target)
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        for name, (model, fields) in indexes.items():
            cursor.execute(f"PRAGMA index_list({quote('curation_' + model)})")
            row = next((row for row in cursor.fetchall() if row[1] == name), None)
            if not row or row[2] or row[3] != "c" or row[4]:
                raise ValueError(f"Missing or unexpected ordinary index: {name}")
            cursor.execute(f"PRAGMA index_info({quote(name)})")
            expected = tuple(
                f"{field}_id" if field in {"collection", "metadata_version"} else field
                for field in (field.lstrip("-") for field in fields)
            )
            if tuple(row[2] for row in cursor.fetchall()) != expected:
                raise ValueError(f"Unexpected index columns: {name}")
            cursor.execute(f"PRAGMA index_xinfo({quote(name)})")
            keys = [row for row in cursor.fetchall() if row[5]]
            if any(
                row[3] != int(field.startswith("-")) or row[4] != "BINARY"
                for row, field in zip(keys, fields, strict=True)
            ):
                raise ValueError(f"Unexpected index ordering or collation: {name}")


def apply(max_seconds=180, progress=None, *, target="collection"):
    import django

    if target not in TARGET_NAMES:
        raise ValueError("Unknown index migration target")
    selected_target = TARGET_NAMES[target]
    indexes = approved_indexes(selected_target)
    if not 1 <= max_seconds <= 1800:
        raise ValueError("max_seconds must be between 1 and 1800")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    started = time.monotonic()
    if connection.vendor != "sqlite":
        raise ValueError("This operational helper is SQLite-only")
    executor = MigrationExecutor(connection)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise ValueError("Foreign key enforcement must be enabled")
    if selected_target in executor.loader.applied_migrations:
        verify_indexes(connection, selected_target)
        return {"status": "already_applied", "target": selected_target}
    plan = executor.migration_plan([selected_target])
    verify_plan(plan, selected_target)
    before = schema_snapshot(connection)
    if any(("index", name) in before for name in indexes):
        raise ValueError("Index exists without migration record; investigate first")
    original_check = connection.check_constraints
    native = connection.connection
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        original_busy_timeout = cursor.fetchone()[0]
        cursor.execute(f"PRAGMA busy_timeout={min(5000, int(max_seconds * 1000))}")
    deadline = time.monotonic() + max_seconds
    state = {"last_report": started, "timed_out": False, "index": None, "closed": False}

    def report(event, **values):
        if progress:
            progress({"event": event, "elapsed_seconds": round(time.monotonic() - started, 3), **values})

    def check_progress():
        now = time.monotonic()
        if now >= deadline:
            state["timed_out"] = True
            return 1
        if now - state["last_report"] >= 10:
            report("index_progress", index=state["index"])
            state["last_report"] = now
        return 0

    def execute_with_progress(execute, sql, params, many, context):
        index = next((name for name in indexes if sql.startswith(f'CREATE INDEX "{name}" ')), None)
        if index:
            state["index"] = index
            report("index_start", index=index)
        result = execute(sql, params, many, context)
        if index:
            report("index_complete", index=index)
        return result

    def check_index_schema(table_names=None):
        # Runs inside the migration's transaction, before it can commit.
        after = schema_snapshot(connection)
        if any(after.get(key) != value for key, value in before.items()):
            raise ValueError("Existing schema changed unexpectedly")
        if set(after) - set(before) != {("index", name) for name in indexes}:
            raise ValueError("Unexpected schema additions")
        verify_indexes(connection, selected_target)

    connection.check_constraints = check_index_schema
    native.set_progress_handler(check_progress, 10000)
    try:
        with connection.execute_wrapper(execute_with_progress):
            executor.migrate([selected_target], plan=plan)
    except BaseException as exc:
        # Includes failure from schema-editor exit; closing rolls back pending DDL.
        native.set_progress_handler(None, 0)
        connection.close()
        state["closed"] = True
        if state["timed_out"]:
            raise RuntimeError("Index migration exceeded time limit and was rolled back") from exc
        raise
    finally:
        connection.check_constraints = original_check
        if not state["closed"] and connection.connection is native:
            native.set_progress_handler(None, 0)
            with connection.cursor() as cursor:
                cursor.execute(f"PRAGMA busy_timeout={original_busy_timeout}")
    verify_indexes(connection, selected_target)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise ValueError("Foreign key enforcement was not restored")
    return {
        "status": "applied",
        "target": selected_target,
        "indexes": list(indexes),
        "existing_schema_unchanged": True,
        "foreign_key_scan": "not_needed_for_index_only_migration",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--target", choices=TARGET_NAMES, default="collection")
    args = parser.parse_args()

    def emit_progress(event):
        print(json.dumps(event), file=sys.stderr, flush=True)

    print(json.dumps(apply(args.max_seconds, emit_progress, target=args.target)), flush=True)
