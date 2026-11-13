#!/usr/bin/env python3
"""Check source-backed display and query behavior against the prepared temporary DB."""

import json
import os
from pathlib import Path

DB = Path("/tmp/biomed-scope-display-isolated-20261112.sqlite3")
if os.environ.get("BIOMED_SCOPE_ISOLATED") != "1" or Path(os.environ.get("DATABASE_PATH", "")) != DB:
    raise SystemExit("Refusing to run without the designated isolated database")
if DB.is_symlink() or not DB.is_file():
    raise SystemExit("The isolated database is missing or is a symlink")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.curation.models import (
    KnowledgeCandidate,
    KnowledgeReview,
    Passage,
)

cases = [
    ("a72d25a4-add8-585c-a328-7a81715767da", "42719457", "therapy_line", "一线治疗", "body/p:9"),
    ("5711e08a-cf02-5cfa-9c9d-235cda2fbe0f", "42756523", "histology", "低级别浆液性卵巢癌",
     "front/article-meta/abstract/p:1"),
]
assert len(cases) == KnowledgeCandidate.objects.count() == 2
assert KnowledgeReview.objects.count() == 0
with connection.cursor() as cursor:
    cursor.execute("PRAGMA query_only = ON")
client = Client(HTTP_HOST="localhost")
records = []
for pk, pmid, field, expected, locator in cases:
    response = client.get(f"/api/v1/curation/knowledge-candidates/{pk}/")
    assert response.status_code == 200
    data = response.json()
    assert data["article"]["pmid"] == pmid
    assert data["publication_status"] == "draft" and data["ai_status"] != "pass"
    presentation = data["evidence_presentation"]
    assert presentation["evidence_level"]["status"] == presentation["drug_effect"]["status"] == "unknown"
    assert presentation["informational_retrieval"] and not presentation["treatment_recommendation"]
    assert not presentation["reported_context"]["automatic_patient_eligibility"]
    requirement = next(item for item in presentation["reported_context"]["requirements"] if item["field"] == field)
    assert requirement["reported_label"] == expected
    source = requirement["source"]
    passage = Passage.objects.get(pk=source["passage"])
    assert source["version_id"] == str(passage.version_id) == data["article"]["version_id"]
    assert source["locator"] == passage.locator == locator
    assert passage.text[source["start"]:source["end"]] == source["quote"]
    assert {item["status"] for item in requirement["choices"]} >= {"consistent", "different"}
    search = client.get("/api/v1/curation/knowledge-candidates/", {"q": pmid}).json()
    assert any(item["id"] == pk for item in search["results"])
    assert client.get("/api/v1/curation/knowledge/", {"q": pmid}).json()["count"] == 0
    records.append({"pmid": pmid, "group_id": data["group_id"], "publication_status": data["publication_status"],
                    "ai_status": data["ai_status"], "grade": "unknown", "drug_effect": "unknown",
                    "field": field, "reported_label": expected, "source": source,
                    "choices": requirement["choices"], "candidate_search": "found", "published_search": "absent"})

query_counts = {}
for limit in (1, 2):
    with CaptureQueriesContext(connection) as captured:
        response = client.get("/api/v1/curation/knowledge-candidates/", {"limit": limit})
        assert response.status_code == 200 and len(response.json()["results"]) == limit
    query_counts[str(limit)] = len(captured)
assert query_counts["1"] == query_counts["2"], query_counts
assert KnowledgeReview.objects.count() == 0
print(json.dumps({"isolated_database_only": True, "database_query_only": True, "tumor_api_contacted": False,
                  "records": records, "page_sql_query_counts": query_counts,
                  "review_records_created": 0}, ensure_ascii=False, indent=2))
