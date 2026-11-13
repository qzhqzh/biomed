"""Capture/resume a versioned RxClass EPC snapshot without touching the DB."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.rxclass_source import fetch_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = fetch_snapshot(args.output)
    print(
        json.dumps(
            {
                "state": manifest["state"],
                "version": manifest["version"],
                "counts": manifest["counts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
