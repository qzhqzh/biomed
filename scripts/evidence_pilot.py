#!/usr/bin/env python3
"""Freeze and inspect a bounded offline pilot without changing production databases."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def setup_helpers():
    import django
    from django.conf import settings

    # Never load project settings/.env or open the production Django connection.
    settings.configure(
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "apps.curation",
        ],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        },
        USE_TZ=True,
        SECRET_KEY="offline-pilot-no-server",
    )
    django.setup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--database", type=Path, required=True)
    freeze_parser.add_argument("--library", type=Path, required=True)
    freeze_parser.add_argument("--output", type=Path, required=True)
    freeze_parser.add_argument("--limit", type=int, default=20)
    selection = freeze_parser.add_mutually_exclusive_group()
    selection.add_argument("--selection-from", type=Path)
    selection.add_argument("--selection-file", type=Path)
    freeze_parser.add_argument("--exposure-root", type=Path)
    freeze_parser.add_argument(
        "--via-worker",
        action="store_true",
        help="Copy only selected sources through existing worker read access",
    )
    run_parser = commands.add_parser("run")
    run_parser.add_argument("folder", type=Path)
    run_parser.add_argument("--max-candidates", type=int, default=100)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    from apps.curation.evidence_pilot import freeze, report, run

    if args.command == "freeze":
        setup_helpers()
        result = freeze(
            args.database,
            args.library,
            args.output,
            args.limit,
            args.selection_from,
            args.exposure_root,
            args.selection_file,
            args.via_worker,
        )
    elif args.command == "run":
        result = run(args.folder, args.max_candidates)
    else:
        result = report(args.folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
