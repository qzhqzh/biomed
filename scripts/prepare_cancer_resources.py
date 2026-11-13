"""Capture versioned OncoTree and reference Chinese labels without DB writes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.cancer_resources import capture, display_overlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--display-overlay", action="store_true")
    args = parser.parse_args()
    if args.display_overlay:
        print(json.dumps(display_overlay(args.output)["counts"], ensure_ascii=False))
        return
    manifest = capture(args.output)
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in ("state", "oncotree_version", "update_status", "counts")
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
