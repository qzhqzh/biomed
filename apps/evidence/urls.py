from django.urls import path

from .views import evidence_api

urlpatterns = [
    path(
        f"{endpoint}/",
        evidence_api,
        {"endpoint": endpoint},
        name=f"evidence-{endpoint}",
    )
    for endpoint in ("health", "catalog", "cases", "extract", "search", "issues")
] + [
    path(
        "issues/export/",
        evidence_api,
        {"endpoint": "issues/export"},
        name="evidence-export",
    ),
    path(
        "issues/<uuid:issue_id>/",
        evidence_api,
        {"endpoint": "issue"},
        name="evidence-issue",
    ),
]
