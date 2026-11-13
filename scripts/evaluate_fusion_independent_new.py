"""Compare frozen fusion adapters on the three isolated new-article outputs.

Counts describe candidate structures, not independent clinical facts or
semantic accuracy.  The latter require the separately frozen source reference.
"""

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from apps.curation.decision import digest as source_digest
from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard
from apps.curation.source_fusion_diagnostic_preview import preview as diagnostic_preview
from apps.curation.source_fusion_pair_preview import preview as treatment_preview
from scripts.knowledge_observation_report import digest

PROJECT = Path(__file__).resolve().parents[1]
DICTIONARY = PROJECT / "var/experiments/target-compatibility-20261101/target-dictionary.json"
GENES = PROJECT / "data/knowledge/genes.jsonl"
PMIDS = {"35664754", "41059188", "35941871"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(root, output):
    root, output = Path(root), Path(output)
    if output.exists():
        raise FileExistsError("Preserve the first independent comparison")
    final = root / "new-batch/final"
    summary = read(final / "summary.json")
    source = read(root / "new-source-snapshot.json")
    reference = read(root / "astra-source-reference.json")
    reference_check = read(root / "astra-source-reference-validation.json")
    manifest = read(final / "source-manifest.json")
    bundle = read(final / "knowledge.json")
    package = read(final / "tumor-api-export.json")
    mappings = read(final / "target-mappings.json")
    dictionary = read(DICTIONARY)
    names = read(root / "diagnostic-name-reference.json")
    if (source.get("sha256") != source_digest(source.get("snapshot"))
            or reference.get("source_only_sha256") != sha(root / "source-only.json")
            or reference_check.get("review_sha256") != sha(root / "astra-source-reference.json")
            or manifest.get("dictionary_sha256") != sha(DICTIONARY)
            or manifest.get("target_written") or package.get("target_written")
            or summary.get("target_written")):
        raise ValueError("Frozen source, independent reference or target dictionary differs")
    docs = {str(doc["pmid"]): doc for doc in source["snapshot"]["documents"]}
    if set(docs) != PMIDS or len(source["snapshot"]["documents"]) != len(docs):
        raise ValueError("Expected exactly the three independently selected articles")
    if {str(item["pmid"]) for item in summary["articles"]} != PMIDS:
        raise ValueError("Batch summary does not cover the frozen articles")
    records = bundle["records"]
    exports = {row["group_id"]: row for row in package["records"]}
    groups = [row["group_id"] for row in records]
    if len(set(groups)) != len(groups) or len(exports) != len(package["records"]):
        raise ValueError("Duplicate candidate groups")
    if set(groups) != set(exports) or set(groups) != set(mappings):
        raise ValueError("Knowledge, exports and mappings do not describe the same candidates")
    genes = load_gene_alias_standard(GENES)
    rows = []
    for record in records:
        pmid, group = str(record["article"]["pmid"]), record["group_id"]
        if pmid not in docs:
            raise ValueError("Candidate article is not in the frozen selection")
        doc = docs[pmid]
        versions = [v for v in doc["versions"] if str(v["id"]).replace("-", "")
                    == str(doc["fulltext_version_id"]).replace("-", "")]
        if (len(versions) != 1 or doc.get("passages_sha256") != source_digest(doc.get("passages"))
                or str(record["version_id"]).replace("-", "")
                != str(doc["fulltext_version_id"]).replace("-", "")
                or record.get("source_sha256") != versions[0].get("sha256")):
            raise ValueError(f"Candidate source lineage differs: {group}")
        old = dict(exports[group])
        old.pop("biomed_companion", None)
        sample = {"pmid": pmid, "group_id": group, "record": record, "original_export": old,
                  "original_mapping": mappings[group], "source_document": doc,
                  "source_snapshot_sha256": source["sha256"], "source_manifest": manifest}
        treatment = treatment_preview(sample, dictionary=dictionary, gene_standard=genes)
        diagnostic = diagnostic_preview(sample, dictionary=dictionary, gene_standard=genes, name_references=names)
        rows.append({"pmid": pmid, "group_id": group, "sample_sha256": digest(sample),
                     "baseline_offline_ready": bool(old.get("offline_ready")),
                     "treatment_preview": treatment, "diagnostic_preview": diagnostic,
                     "record": record})
    by_article = {}
    for pmid in sorted(PMIDS):
        article = [row for row in rows if row["pmid"] == pmid]
        by_article[pmid] = {
            "candidates": len(article),
            "baseline_offline_ready": sum(row["baseline_offline_ready"] for row in article),
            "treatment_previews": sum(row["treatment_preview"]["status"] == "inactive_structure_preview" for row in article),
            "diagnostic_previews": sum(row["diagnostic_preview"]["status"] == "inactive_structure_preview" for row in article),
            "treatment_reasons": dict(Counter(row["treatment_preview"]["reason"] for row in article)),
            "diagnostic_reasons": dict(Counter(row["diagnostic_preview"]["reason"] for row in article)),
            "extraction": next(item["extraction"] for item in summary["articles"] if str(item["pmid"]) == pmid),
        }
    result = {
        "version": "fusion-independent-new-comparison-v1", "articles": len(docs),
        "batch_state": summary["state"], "candidates": len(rows), "per_article": by_article,
        "records": rows, "reference_sha256": sha(root / "astra-source-reference.json"),
        "source_snapshot_sha256": sha(root / "new-source-snapshot.json"),
        "knowledge_sha256": sha(final / "knowledge.json"),
        "export_sha256": sha(final / "tumor-api-export.json"),
        "treatment_code_sha256": sha(PROJECT / "apps/curation/source_fusion_pair_preview.py"),
        "diagnostic_code_sha256": sha(PROJECT / "apps/curation/source_fusion_diagnostic_preview.py"),
        "diagnostic_name_reference_sha256": sha(root / "diagnostic-name-reference.json"),
        "scope": "Candidate and unreviewed structural counts only; distinct facts and accuracy need source review.",
        "production_written": False, "target_written": False,
    }
    # Publish only a complete file; an interrupted comparison can resume
    # without mistaking a partially written result for a frozen experiment.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".fusion-comparison-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)  # Atomic publication without overwriting a prior result.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"articles": len(docs), "candidates": len(rows), "per_article": by_article}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output), ensure_ascii=False))
