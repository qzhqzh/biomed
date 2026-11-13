"""Inspect or register research records without touching models or the database."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.research_history import (
    DIRECTORY,
    find_identity,
    records_index,
    register_record,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "check", "register"))
    parser.add_argument("file", type=Path, nargs="?")
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    args = parser.parse_args()
    if args.action != "list" and args.file is None:
        parser.error("check/register require a JSON file")
    try:
        if args.action == "list":
            result = records_index(args.directory)
        else:
            value = json.loads(args.file.read_text(encoding="utf-8"))
            result = (
                find_identity(value.get("identity", value), args.directory)
                if args.action == "check"
                else register_record(value, args.directory)
            )
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Research record rejected: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
