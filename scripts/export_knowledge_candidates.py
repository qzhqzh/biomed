"""Export an existing knowledge bundle to an offline tumor_api compatibility package."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.knowledge_export import export_bundle  # noqa: E402


def read(path):
    return json.loads(path.read_text()) if path else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--dictionary", type=Path, help="Read-only target dictionary snapshot; never a model-generated ID list")
    parser.add_argument("--mappings", type=Path, help="Object keyed by group_id: entity_ids, trigger_scope, treatment_scope, trigger_bindings and original source excerpts")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = export_bundle(read(args.bundle), dictionary=read(args.dictionary), mappings=read(args.mappings))
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output.exists():
        if args.output.read_text() != text:
            raise ValueError("Refusing to overwrite a different export; choose a new version")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(text)
    print(json.dumps({"output": str(args.output), "counts": result["counts"],
                      "offline_ready_count": result["offline_ready_count"], "submit_ready": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
