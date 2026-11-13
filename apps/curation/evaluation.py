"""Offline evaluation of frozen, explicitly reviewed literature samples.

No database writes, acquisition, inference, or training. Agreement with provisional
assistant references is not clinical accuracy or a human-adjudicated gold score.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from apps.curation.classification import classify_metadata, taxonomy_hash

SCOPES = {
    "solid_candidate",
    "hematologic_candidate",
    "mixed_candidate",
    "solid_boundary_candidate",
    "benign_candidate",
    "precursor_candidate",
    "uncertain",
}
STATUSES = {
    "unmarked",
    "retraction_notice",
    "retracted_publication",
    "correction",
    "expression_of_concern",
    "conflicting_source_statuses",
}


def input_hash(metadata):
    return hashlib.sha256(
        json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def load_reviewed_samples(paths, manifest_path):
    """Reject changed input, duplicate/related samples and unreviewed references."""
    manifest = json.loads(Path(manifest_path).read_text())
    frozen = {row["pmid"]: row for row in manifest["records"]}
    if len(frozen) != len(manifest["records"]):
        raise ValueError("抽样清单含重复 PMID")
    groups = {}
    for row in frozen.values():
        if row["group_id"] in groups:
            raise ValueError("抽样清单含重复关联组，可能泄漏")
        groups[row["group_id"]] = row["split"]
    samples = []
    seen = set()
    for path in paths:
        data = json.loads(Path(path).read_text())
        if data["protocol"] != manifest["protocol"]:
            raise ValueError("评测协议与抽样清单不符")
        for row in data["records"]:
            pmid = row["pmid"]
            if pmid in seen:
                raise ValueError(f"重复评测 PMID {pmid}")
            seen.add(pmid)
            original = frozen.get(pmid)
            if not original or any(row.get(k) != v for k, v in original.items()):
                raise ValueError(f"PMID {pmid} 的版本、输入或分组与抽样清单不符")
            if row["split"] not in ("development", "holdout"):
                raise ValueError("未知的评测分组")
            metadata = row["metadata"]
            if (
                set(metadata) != {"title", "abstract", "mesh", "publication_types"}
                or not all(isinstance(metadata[k], str) for k in ("title", "abstract"))
                or not all(
                    isinstance(metadata[k], list)
                    and all(isinstance(value, str) for value in metadata[k])
                    for k in ("mesh", "publication_types")
                )
                or input_hash(metadata) != row["input_sha256"]
            ):
                raise ValueError(f"PMID {pmid} 的元数据校验失败")
            reference = row.get("reference", {})
            if (
                reference.get("review_status") != "assistant_reviewed_provisional"
                or reference.get("human_reviewed") is not False
                or not reference.get("annotator")
                or not reference.get("rationale")
                or not reference.get("relevance")
                or not reference.get("reviewed_at")
                or reference.get("scope") not in SCOPES
                or reference.get("publication_status") not in STATUSES
                or not reference.get("evidence")
            ):
                raise ValueError(f"PMID {pmid} 缺少有效的暂定核读标签与依据")
            for evidence in reference["evidence"]:
                field = evidence["field"]
                value = metadata.get(field)
                if isinstance(value, list):
                    value = " ; ".join(value)
                start, end = evidence["start"], evidence["end"]
                if (
                    not isinstance(value, str)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not 0 <= start < end <= len(value)
                    or value[start:end] != evidence["text"]
                ):
                    raise ValueError(f"PMID {pmid} 的引用偏移或原文不符")
            samples.append(row)
    # A split must be complete; silently dropping hard cases biases results.
    splits = {row["split"] for row in samples}
    expected = {pmid for pmid, row in frozen.items() if row["split"] in splits}
    if not samples or seen != expected:
        raise ValueError("必须提交完整分组，不能跳过未标注或困难样本")
    return manifest["protocol"], samples


def fraction(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": numerator / denominator if denominator else None,
    }


def score_samples(samples, taxonomy):
    predictions = []
    confusion = Counter()
    for row in samples:
        predicted = classify_metadata(row["metadata"], taxonomy)
        ref = row["reference"]
        confusion[ref["scope"], predicted["scope"]] += 1
        predictions.append(
            {
                "pmid": row["pmid"],
                "scope": predicted["scope"],
                "reference_scope": ref["scope"],
                "scope_agrees": predicted["scope"] == ref["scope"],
                "publication_status": predicted.get("publication_status", "unmarked"),
                "reference_publication_status": ref["publication_status"],
                "label_ids": [label["id"] for label in predicted["labels"]],
                "suppressed_matches": predicted.get("suppressed_matches", []),
                "uncertainty_reason": predicted.get("uncertainty_reason"),
            }
        )
    decided = [row for row in predictions if row["scope"] != "uncertain"]
    known = [row for row in predictions if row["reference_scope"] != "uncertain"]
    per_scope = {}
    for scope in sorted(SCOPES):
        support = sum(count for (ref, _), count in confusion.items() if ref == scope)
        predicted = sum(
            count for (_, pred), count in confusion.items() if pred == scope
        )
        correct = confusion[scope, scope]
        per_scope[scope] = {
            "support": support,
            "predicted": predicted,
            "precision": fraction(correct, predicted),
            "recall": fraction(correct, support),
        }
    return {
        "taxonomy_version": taxonomy["version"],
        "taxonomy_sha256": taxonomy_hash(taxonomy),
        "scope_agreement": fraction(
            sum(r["scope_agrees"] for r in predictions), len(samples)
        ),
        "decision_coverage": fraction(len(decided), len(samples)),
        "decided_scope_agreement": fraction(
            sum(r["scope_agrees"] for r in decided), len(decided)
        ),
        "known_scope_agreement": fraction(
            sum(r["scope_agrees"] for r in known), len(known)
        ),
        "publication_status_agreement": fraction(
            sum(
                r["publication_status"] == r["reference_publication_status"]
                for r in predictions
            ),
            len(samples),
        ),
        "per_scope": per_scope,
        "confusion": [
            {"reference": ref, "predicted": pred, "count": count}
            for (ref, pred), count in sorted(confusion.items())
        ],
        "predictions": predictions,
    }


def evaluate_samples(protocol, samples, taxonomies):
    return {
        "dataset_id": protocol["dataset_id"],
        "reference_status": "assistant_reviewed_provisional",
        "warning": "助手单人核读的暂定参考标签；分层富集样本，非总体准确率，也非人工金标准。",
        "evaluated_axes": ["scope", "publication_status"],
        "unassessed_axes": ["site", "histology", "study_type", "topic", "population"],
        "splits": {
            split: {
                "count": len(subset),
                "relevance_counts": dict(
                    Counter(r["reference"]["relevance"] for r in subset)
                ),
                "needs_adjudication": [
                    r["pmid"]
                    for r in subset
                    if r["reference"].get("needs_adjudication")
                ],
                "scores": [score_samples(subset, taxonomy) for taxonomy in taxonomies],
            }
            for split in sorted({row["split"] for row in samples})
            if (subset := [row for row in samples if row["split"] == split])
        },
    }
