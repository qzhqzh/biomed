"""Explore ordered-fusion mapping on the already reviewed development cases."""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_fusion_pair_preview import preview
from scripts.knowledge_observation_report import digest

PROJECT = Path(__file__).resolve().parents[1]
DICTIONARY = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
GENES = PROJECT / "data/knowledge/genes.jsonl"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate(root, output):
    started = time.perf_counter()
    root, output = Path(root), Path(output)
    if output.exists():
        raise FileExistsError("Development comparison must not overwrite previous output")
    manifest, samples = read(root / "manifest.json"), read(root / "samples.json")
    reference_validation = read(root / "astra-baseline-review-validation.json")
    if (digest(samples) != manifest["samples_sha256"] or len(samples) != 58
            or reference_validation["review_sha256"] != sha(root / "astra-baseline-review.json")):
        raise ValueError("Samples or independently frozen review differ")
    dictionary, standard = read(DICTIONARY), load_gene_alias_standard(GENES)
    dictionary_sha = sha(DICTIONARY)
    results = []
    for sample in samples:
        if (digest({k: v for k, v in sample.items() if k != "sample_sha256"}) != sample["sample_sha256"]
                or sample["source_manifest"].get("dictionary_sha256") != dictionary_sha
                or (sample["source_manifest"].get("gene_standard_sha256") is not None
                    and sample["source_manifest"]["gene_standard_sha256"] != standard["source"]["sha256"])):
            raise ValueError(f"Frozen sample or dictionary differs: {sample['group_id']}")
        old_ready = bool(sample["original_export"].get("offline_ready"))
        if old_ready:
            result = {"status": "baseline_preserved", "reason": "original_offline_ready_preserved"}
        else:
            result = preview(sample, dictionary=dictionary, gene_standard=standard)
        results.append({"pmid": sample["pmid"], "group_id": sample["group_id"],
                        "sample_sha256": sample["sample_sha256"], "baseline_offline_ready": old_ready,
                        "preview": result})
    passed = [row for row in results if row["preview"]["status"] == "inactive_structure_preview"]
    if any(row["preview"].get("automatic_clinical_matching") or row["preview"].get("submit_ready")
           or row["preview"]["target_payload"].get("is_active") for row in passed):
        raise ValueError("Development previews must be inactive and not submit-ready")
    report = {"version": "source-fusion-pair-development-v1", "source_only_sha256": manifest["source_sha256"],
              "sample_count": len(samples), "article_count": len({row["pmid"] for row in samples}),
              "baseline_offline_count": sum(row["baseline_offline_ready"] for row in results),
              "inactive_structure_preview_count": len(passed),
              "reason_counts": dict(Counter(row["preview"]["reason"] for row in results)),
              "code_sha256": sha(PROJECT / "apps/curation/source_fusion_pair_preview.py"),
              "dictionary_sha256": dictionary_sha, "gene_standard_sha256": standard["source"]["sha256"],
              "astra_baseline_review_sha256": reference_validation["review_sha256"],
              "development_after_reference_review": True,
              "scope_note": "Exploratory development on inspected cases, not a blinded holdout or article-level recall.",
              "semantic_status": "new_payload_requires_independent_review", "production_connected": False,
              "target_written": False, "automatic_clinical_matching": False,
              "elapsed_seconds": round(time.perf_counter() - started, 3), "records": results}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return {key: report[key] for key in ("sample_count", "baseline_offline_count", "inactive_structure_preview_count",
                                        "reason_counts", "elapsed_seconds")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.root, args.output), ensure_ascii=False))
