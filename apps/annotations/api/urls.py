from django.urls import path

from apps.annotations.api.views import (
    AnnotationsView,
    EnginesView,
    JobDetailView,
    JobsView,
    KnowledgeGenesView,
    KnowledgeMetaView,
    KnowledgeTranscriptsView,
    KnowledgeUpdatesView,
    LegacyAnnotateView,
)

urlpatterns = [
    path("engines/", EnginesView.as_view(), name="engines"),
    path("annotations/", AnnotationsView.as_view(), name="annotations"),
    path("jobs/", JobsView.as_view(), name="jobs"),
    path("jobs/<uuid:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("knowledge/meta/", KnowledgeMetaView.as_view(), name="knowledge-meta"),
    path("knowledge/updates/", KnowledgeUpdatesView.as_view(), name="knowledge-updates"),
    path("knowledge/genes/", KnowledgeGenesView.as_view(), name="knowledge-genes"),
    path(
        "knowledge/transcripts/",
        KnowledgeTranscriptsView.as_view(),
        name="knowledge-transcripts",
    ),
    # Temporary FastAPI compatibility
    path("legacy/annotate", LegacyAnnotateView.as_view(), name="legacy-annotate"),
]
