#!/usr/bin/env python3
"""Evaluate frozen literature pilots against an independent offline model reference."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from apps.curation.pilot_evaluation import evaluate

    try:
        result = evaluate(args.experiment, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Evaluation refused: {exc}\n")
    print(
        json.dumps(
            {
                "state": result["state"],
                "reference_status": result["reference_status"],
                "cohorts": result["cohorts"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
