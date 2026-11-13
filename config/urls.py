from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from apps.portal.access import WorkbenchLoginView

urlpatterns = [
    path("accounts/login/", WorkbenchLoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path("api/v1/curation/", include("apps.curation.urls")),
    path("api/v1/evidence/", include("apps.evidence.urls")),
    path("admin/", admin.site.urls),
    path("health/", include("apps.annotations.api.health_urls")),
    path("api/v1/", include("apps.annotations.api.urls")),
    path("", include("apps.portal.urls")),
]
