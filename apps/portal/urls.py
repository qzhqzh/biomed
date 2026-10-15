from django.urls import path

from apps.portal import views

urlpatterns = [
    path(
        "knowledge/diseases/",
        views.standards_page,
        {"namespace": "MONDO"},
        name="diseases",
    ),
    path(
        "knowledge/drugs/", views.standards_page, {"namespace": "RXNORM"}, name="drugs"
    ),
    path("evidence/clinical/", views.clinical_evidence_page, name="evidence-clinical"),
    path("evidence/sources/", views.library_page, name="evidence-sources"),
    path(
        "evidence/sources/papers/", views.library_page, {"kind": "paper"}, name="papers"
    ),
    path(
        "evidence/sources/guidelines/",
        views.library_page,
        {"kind": "guideline"},
        name="guidelines",
    ),
    path("management/workflows/", views.workflows_page, name="management-workflows"),
    path("knowledge/evidence/", views.clinical_evidence_page, name="clinical-evidence"),
    path("knowledge/standards/", views.standards_page, name="standards"),
    path("library/", views.library_page, name="library"),
    path("workflows/", views.workflows_page, name="workflows"),
    path("tools/teaching/", views.evidence_page, name="teaching"),
    path("knowledge/issues/", views.issues_page, name="issues"),
    path("", views.home, name="home"),
    path("tools/annotate/", views.annotate_tool, name="annotate-tool"),
    path("tools/jobs/", views.jobs_page, name="jobs-page"),
    path("tools/engines/", views.engines_page, name="engines-page"),
    path("tools/data-updates/", views.data_updates_page, name="data-updates"),
    path("docs/", views.docs_page, name="docs-page"),
    path("knowledge/genes/", views.gene_knowledge_page, name="gene-knowledge"),
    path(
        "knowledge/amino-acids/",
        views.amino_acid_knowledge_page,
        name="amino-acid-knowledge",
    ),
]
