"""Read back the 15-group release pilot and verify actual AND search and sources."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / "var/experiments/knowledge-release-20261030")
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.test import RequestFactory

    from apps.curation.knowledge_publication import build_review_packet
    from apps.curation.knowledge_search import knowledge, knowledge_data
    from apps.curation.knowledge_storage import digest
    from apps.curation.models import (
        KnowledgeAnchor,
        KnowledgeCandidate,
        KnowledgeReview,
        KnowledgeSearchTerm,
    )

    inputs = json.loads((args.root / "review-inputs.json").read_text())["records"]
    bundle = json.loads((PROJECT / "var/experiments/knowledge-intake-20261030/bundle.json").read_text())
    old = {r["group_id"]: r for r in bundle["records"]}
    candidates = []
    for row in inputs:
        packet = row["packet"]
        candidate = KnowledgeCandidate.objects.select_related("version__material", "current_review").get(pk=packet["candidate"]["id"])
        assert digest(build_review_packet(candidate, packet["extra_passage_ids"])) == row["input_sha256"]
        assert candidate.fact == old[candidate.group_id]["fact"]
        assert candidate.assessment == old[candidate.group_id]["assessment"]
        assert candidate.current_review and candidate.current_review.input_sha256 == row["input_sha256"]
        assert candidate.human_status == "unchecked", "Do not fabricate human audit"
        assert (candidate.publication_status == "published") == (candidate.ai_status == "pass")
        candidates.append(candidate)
    assert len(candidates) == 15
    anchors = KnowledgeAnchor.objects.filter(candidate__in=candidates).select_related("passage")
    for a in anchors:
        assert a.passage.text[a.start:a.end] == a.quote
        assert a.passage.version_id == a.candidate.version_id
    factory = RequestFactory()
    queries = [
        ({"disease": "胆道癌", "drug": "durvalumab", "biomarker": "CRP"}, ["K08", "K09"]),
        ({"disease": "biliary tract cancer", "drug": "DURVALUMAB", "biomarker": "CRP"}, ["K08", "K09"]),
        ({"disease": "胆道癌", "drug": "durvalumab", "biomarker": "NLR"}, []),
        ({"disease": "胆道癌", "drug": "durvalumab", "biomarker": "CRP kinetics"}, ["K09"]),
        ({"disease": "胆道癌", "drug": "ICI", "biomarker": "CRP"}, []),
        ({"disease": "breast cancer", "drug": "durvalumab", "biomarker": "CRP"}, []),
    ]
    query_results = []
    for params, expected in queries:
        response = knowledge(factory.get("/api/v1/curation/knowledge/", params))
        body = json.loads(response.content)
        actual = sorted(r["group_id"] for r in body["results"])
        assert response.status_code == 200 and actual == expected, (params, actual, expected)
        query_results.append({"query": params, "groups": actual, "count": body["count"]})
    all_results = json.loads(knowledge(factory.get("/api/v1/curation/knowledge/")).content)
    assert all_results["count"] == sum(c.publication_status == "published" for c in candidates)
    review_ids = [c.current_review_id for c in candidates]
    result = {
        "passed": True, "candidate_count": len(candidates),
        "ai_status": dict(Counter(c.ai_status for c in candidates)),
        "publication_status": dict(Counter(c.publication_status for c in candidates)),
        "human_status": dict(Counter(c.human_status for c in candidates)),
        "original_facts_and_assessments_unchanged": True, "frozen_inputs_unchanged": True,
        "exact_original_anchors": anchors.count(),
        "new_reviews": KnowledgeReview.objects.filter(id__in=review_ids).count(),
        "search_terms": KnowledgeSearchTerm.objects.filter(review_id__in=review_ids).count(),
        "queries": query_results,
        "records": [knowledge_data(c, history=True) for c in candidates],
    }
    (args.root / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
