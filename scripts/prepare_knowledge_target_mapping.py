"""Prepare source-supported mappings and offline acceptance without target access."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.knowledge_export import export_bundle
from apps.curation.knowledge_target_mapping import load_gene_standard, prepare_mappings


def read(path):
    return json.loads(path.read_text()) if path else None


def freeze(path, value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text() != content:
            raise ValueError(f"Refusing to overwrite different output: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--dictionary", required=True, type=Path, help="Frozen target snapshot")
    parser.add_argument("--registry", type=Path, help="Optional read-only local registry snapshot JSON")
    parser.add_argument("--gene-standard", type=Path, default=ROOT / "data/knowledge/genes.jsonl",
                        help="Existing local NCBI gene JSONL; exact official symbols only")
    parser.add_argument("--output", required=True, type=Path, help="Mapping JSON for export_knowledge_candidates.py")
    parser.add_argument("--report", type=Path, help="Detailed mapping and offline acceptance report")
    parser.add_argument("--export", type=Path, help="Optional complete offline export package")
    args = parser.parse_args()
    bundle, dictionary, registry = read(args.bundle), read(args.dictionary), read(args.registry)
    mappings, report = prepare_mappings(bundle, dictionary, registry, gene_standard=load_gene_standard(args.gene_standard))
    package = export_bundle(bundle, dictionary=dictionary, mappings=mappings) if args.export else None
    # Preflight all destinations before writing any so a collision fails safely.
    outputs = [(args.output, mappings)]
    if args.report:
        outputs.append((args.report, report))
    if args.export:
        outputs.append((args.export, package))
    if len({path.resolve() for path, _ in outputs}) != len(outputs):
        raise ValueError("Mapping, report and export destinations must differ")
    for path, value in outputs:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if path.exists() and path.read_text() != content:
            raise ValueError(f"Refusing to overwrite different output: {path}")
    for path, value in outputs:
        freeze(path, value)
    print(json.dumps({"output": str(args.output), "counts": report["counts"],
                      "gap_counts": report["gap_counts"], "offline_ready_count": report["offline_ready_count"],
                      "target_contacted": False, "target_written": False, "submit_ready": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
