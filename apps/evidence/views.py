"""Same-origin boundary for the existing internal evidence service.

Only explicit routes are forwarded. Never forward cookies, credentials, case
text to environment proxies, or upstream redirects to another destination.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.views.decorators.cache import never_cache


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(ProxyHandler({}), NoRedirects())
_METHODS = {
    "health": {"GET"},
    "catalog": {"GET"},
    "cases": {"GET"},
    "extract": {"POST"},
    "search": {"POST"},
    "issues": {"GET", "POST"},
    "issues/export": {"GET"},
    "issue": {"PATCH"},
}


@never_cache
def evidence_api(request, endpoint, issue_id=None):
    methods = _METHODS.get(endpoint, set())
    if request.method not in methods:
        return HttpResponseNotAllowed(sorted(methods))
    body = None
    if request.method in {"POST", "PATCH"}:
        if request.content_type != "application/json":
            return JsonResponse({"detail": "请使用 JSON 提交"}, status=415)
        if len(request.body) > 32768:
            return JsonResponse({"detail": "请求内容过大"}, status=413)
        body = request.body
    path = f"issues/{issue_id}" if endpoint == "issue" else endpoint
    upstream = Request(
        f"{settings.EVIDENCE_API_URL.rstrip('/')}/api/{path}",
        data=body,
        method=request.method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        try:
            response = _opener.open(upstream, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            content = response.read()
            if not 200 <= status < 500 or 300 <= status < 400:
                raise ValueError("Unexpected upstream status")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Unexpected upstream content type")
            json.loads(content)
    except (OSError, URLError, ValueError):
        return JsonResponse(
            {"detail": "资料服务暂时不可用，请稍后重试。已有记录仍保留。"},
            status=503,
        )
    result = HttpResponse(content, status=status, content_type="application/json")
    if endpoint == "issues/export" and status == 200:
        result["Content-Disposition"] = 'attachment; filename="learning-log.json"'
    return result
