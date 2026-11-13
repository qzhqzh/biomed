"""Permissions and throttles for annotation APIs."""

from __future__ import annotations

from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from apps.annotations.api.auth import api_key_required


class HasAPIKeyOrOpen(BasePermission):
    """Allow if API key auth succeeded, or if API key is not required (dev open mode)."""

    def has_permission(self, request, view):
        if not api_key_required():
            return True
        return bool(request.auth)


class AnnotationRateThrottle(SimpleRateThrottle):
    scope = "annotation"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        if request.auth:
            ident = f"key:{str(request.auth)[:16]}"
        elif getattr(request, "biomed_authenticated_user_id", None) is not None:
            ident = f"user:{request.biomed_authenticated_user_id}"
        return self.cache_format % {"scope": self.scope, "ident": ident}
