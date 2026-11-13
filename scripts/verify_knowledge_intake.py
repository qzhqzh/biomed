"""Read back a candidate import and verify facts, exact quotes and original sources."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def verify(root):
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from apps.curation.knowledge_storage import import_candidates
    from apps.curation.models import (
        Evidence,
        KnowledgeAnchor,
        KnowledgeCandidate,
        KnowledgeEntity,
        Review,
    )
    from scripts.prepare_knowledge_intake import freeze, read, sha

    bundle = read(root / "bundle.json")
    # Validates every source and detects a same-run mismatch without writing.
    reused = import_candidates(bundle, dry_run=True)
    if not reused["reused"]:
        raise ValueError("No actual import exists")
    candidates = KnowledgeCandidate.objects.filter(source_run=bundle["source_run"])
    by_group = {c.group_id: c for c in candidates}
    anchors = KnowledgeAnchor.objects.filter(candidate__in=candidates).select_related("passage", "entity", "candidate__version")
    expected_anchors = sum(len(row["anchors"]) for row in bundle["records"])
    if anchors.count() != expected_anchors:
        raise ValueError("Anchor count changed")
    for row in bundle["records"]:
        candidate = by_group[row["group_id"]]
        if candidate.fact != row["fact"] or candidate.assessment != row["assessment"] or candidate.status != "research_candidate":
            raise ValueError("Original fact, reference or candidate state changed")
    for anchor in anchors:
        if anchor.passage.version_id != anchor.candidate.version_id:
            raise ValueError("Cross-version anchor")
        if anchor.passage.text[anchor.start:anchor.end] != anchor.quote:
            raise ValueError("Original English changed")
        if anchor.entity_id and anchor.entity.scope_version_id != anchor.passage.version_id:
            raise ValueError("Cross-version entity scope")
    entities = KnowledgeEntity.objects.filter(anchors__candidate__in=candidates).distinct()
    # These tables were empty before this task and must remain untouched by intake.
    report = {"passed": True, "source_run": bundle["source_run"], "candidate_count": len(by_group),
              "entity_count": entities.count(), "entity_kinds": dict(Counter(entities.values_list("kind", flat=True))),
              "anchor_count": anchors.count(), "source_versions": candidates.values("version_id").distinct().count(),
              "facts_unchanged": True, "assessments_unchanged": True, "english_spans_exact": True,
              "idempotent_replay": reused, "formal_evidence_count": Evidence.objects.count(),
              "formal_review_count": Review.objects.count(), "bundle_sha256": sha(root / "bundle.json")}
    before = read(PROJECT / "var/experiments" / "knowledge-intake-20261030" / "before-counts.json")
    if report["formal_evidence_count"] != before["formal_evidence_count"] or report["formal_review_count"] != before["formal_review_count"]:
        raise ValueError("Formal evidence/review count differs from the before snapshot")
    freeze(root / "verification.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / "var/experiments/knowledge-intake-20261030")
    args = parser.parse_args()
    result = verify(args.root)
    print(json.dumps({k: v for k, v in result.items() if k != "idempotent_replay"}, ensure_ascii=False))
