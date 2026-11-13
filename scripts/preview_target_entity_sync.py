"""Exercise automatic drug creation and evidence-ID binding in an isolated DB."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.knowledge_export import digest, export_bundle
from apps.curation.knowledge_target_mapping import (
    METHOD_VERSION,
    load_gene_standard,
    prepare_mappings,
)
from apps.curation.target_entity_sync import PreviewTarget, preview_entity_sync
from scripts.prepare_knowledge_target_mapping import freeze, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--preview-database", type=Path, required=True,
                        help="Dedicated preview SQLite file, never an application database; reuse to verify retries")
    parser.add_argument("--output", type=Path, required=True, help="New result directory; use a new version when replaying")
    parser.add_argument("--gene-standard", type=Path, default=ROOT / "data/knowledge/genes.jsonl")
    args = parser.parse_args()
    files = ("plan.json", "mappings.json", "receipt.json", "preview-export.json", "summary.json")
    if any((args.output / filename).exists() for filename in files):
        raise ValueError("Choose a new result directory; the same preview database can be safely reused")
    bundle, dictionary, registry = read(args.bundle), read(args.dictionary), read(args.registry)
    mappings, report = prepare_mappings(bundle, dictionary, registry, gene_standard=load_gene_standard(args.gene_standard))
    package = export_bundle(bundle, dictionary=dictionary, mappings=mappings)
    target = PreviewTarget(args.preview_database, dictionary)
    try:
        bound, receipt = preview_entity_sync(mappings, target)
        preview = export_bundle(bundle, dictionary=target.snapshot(), mappings=bound)
    finally:
        target.close()
    summary = {"mapping_method_version": METHOD_VERSION, "source_run": bundle["source_run"],
               "source_bundle_sha256": digest(bundle), "registry_sha256": digest(registry),
               "dictionary_sha256": digest(dictionary), "preview_database": str(args.preview_database.resolve()),
               "automatic_operations": len(package["entity_operations"]),
               "created_count": receipt["created_count"], "reused_count": receipt["reused_count"],
               "failed_count": len(receipt["failures"]), "before_counts": report["counts"],
               "after_preview_counts": preview["counts"], "preview_ready_count": preview["preview_ready_count"],
               "preview_only": True, "target_contacted": False, "target_written": False, "submit_ready": False}
    for filename, value in zip(files, (package, mappings, receipt, preview, summary), strict=True):
        freeze(args.output / filename, value)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
