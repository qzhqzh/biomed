"""Session login for the workbench, with an opt-in single access level."""

import hashlib

from django.conf import settings
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.views import LoginView, redirect_to_login
from django.core.cache import cache
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.utils.cache import patch_cache_control


class WorkbenchPermissionsBackend(BaseBackend):
    """When enabled, every active workbench user can perform evidence review."""

    def has_perm(self, user_obj, perm, obj=None):
        return bool(
            settings.BIOMED_ALL_AUTHENTICATED_CAN_REVIEW
            and obj is None
            and perm == "curation.change_evidence"
            and user_obj.is_authenticated
            and user_obj.is_active
        )


class RequireWorkbenchLoginMiddleware:
    """Protect pages and APIs while leaving the login and health probes available."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.BIOMED_REQUIRE_LOGIN:
            return self.get_response(request)

        path = request.path_info
        public = path == "/accounts/login/" or (
            path in {"/health/live", "/health/ready"}
            and request.method in {"GET", "HEAD"}
        )
        if request.user.is_authenticated:
            # DRF's API-key authenticator may replace request.user later.
            # Preserve only the verified Django session identity for limits.
            request.biomed_authenticated_user_id = request.user.pk
        if not public and not request.user.is_authenticated:
            if path.startswith("/api/"):
                response = JsonResponse({"error": "请先登录后使用工作台。"}, status=401)
            else:
                response = redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        else:
            response = self.get_response(request)
        patch_cache_control(response, private=True, no_store=True)
        return response


def _csrf_checked_view(*args, **kwargs):
    """A non-exempt marker used to check session-authenticated DRF views."""


class EnforceApiSessionCsrfMiddleware:
    """DRF API-key views are csrf_exempt; browser sessions must still be protected."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.csrf = CsrfViewMiddleware(get_response)

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if (
            settings.BIOMED_REQUIRE_LOGIN
            and request.path_info.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and request.user.is_authenticated
        ):
            return self.csrf.process_view(
                request, _csrf_checked_view, callback_args, callback_kwargs
            )
        return None


class WorkbenchLoginView(LoginView):
    template_name = "portal/login.html"
    redirect_authenticated_user = True
    failure_limit = 5
    failure_window_seconds = 300

    def failure_key(self):
        # Per account rather than per proxy address: one visitor's mistakes
        # must not lock every user behind the same public gateway.
        username = self.request.POST.get("username", "")[:150].strip().casefold()
        identity = hashlib.sha256(username.encode("utf-8")).hexdigest()
        return f"biomed-workbench-login:{identity}"

    def post(self, request, *args, **kwargs):
        if cache.get(self.failure_key(), 0) >= self.failure_limit:
            form = self.get_form_class()(self.request)
            return self.render_to_response(
                self.get_context_data(form=form, rate_limited=True), status=429
            )
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = self.failure_key()
        cache.add(key, 0, timeout=self.failure_window_seconds)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=self.failure_window_seconds)
        return super().form_invalid(form)

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session.set_expiry(8 * 60 * 60)
        cache.delete(self.failure_key())
        return response
