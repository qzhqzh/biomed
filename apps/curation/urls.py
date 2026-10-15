from django.urls import path

from apps.curation import views

urlpatterns = [
    path("overview/", views.overview),
    path("sources/", views.sources),
    path("concepts/", views.concepts),
    path("materials/", views.materials),
    path("materials/<uuid:pk>/", views.material_detail),
    path("versions/<uuid:pk>/download/", views.download),
    path("versions/<uuid:pk>/processing/", views.version_processing),
    path("pubmed/<str:action>/", views.pubmed),
    path("workflows/", views.workflows),
    path("workflows/<uuid:pk>/", views.workflow_detail),
    path("evidence/", views.evidence),
    path("evidence/<uuid:pk>/", views.evidence_detail),
    path("session/", views.session),
]
