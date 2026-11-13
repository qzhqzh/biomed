#!/usr/bin/env python3
"""Download/resume only ChEMBL mechanisms and their referenced entities."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.chembl_source import fetch_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    manifest = fetch_snapshot(args.output_dir)
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "state",
                    "version",
                    "release_date",
                    "counts",
                    "completed_at",
                    "charged_bytes",
                    "request_attempts",
                    "cjk_synonym_records",
                    "cjk_synonym_molecules",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
