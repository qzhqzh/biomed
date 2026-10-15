import json
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from apps.curation.materials import upload_pdf
from apps.curation.models import (
    Concept,
    Evidence,
    Material,
    MaterialVersion,
    SourceRelease,
    WorkerHeartbeat,
    WorkItem,
)
from apps.curation.network import SourceBlocked
from apps.curation.review import human_review
from apps.curation.storage import blob_path
from apps.curation.workflows import enqueue, retry_job


def api(methods):
    def decorate(func):
        @require_http_methods(methods)
        @wraps(func)
        def handle(request, *args, **kwargs):
            try:
                response = func(request, *args, **kwargs)
                response["Cache-Control"] = "no-store"
                return response
            except (ValueError, ValidationError) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, SourceBlocked)
                    else "请求格式无效，请检查输入。"
                )
                return JsonResponse({"error": message}, status=400)
            except PermissionError as exc:
                return JsonResponse({"error": str(exc)}, status=403)

        return handle

    return decorate


def body(request):
    if request.content_type != "application/json" or len(request.body) > 32768:
        raise SourceBlocked("请使用小于 32 KB 的 JSON 请求")
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise SourceBlocked("请求内容必须为对象")
    return value


def field(data, key, maximum=1000, optional=False, strip=True):
    value = data.get(key, "")
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value.strip() and not optional)
    ):
        raise SourceBlocked(f"{key} 缺失或超出长度限制")
    return value.strip() if strip else value


def page(request):
    return max(0, min(int(request.GET.get("offset", 0)), 100000)), max(
        1, min(int(request.GET.get("limit", 50)), 100)
    )


def job_data(job):
    return {
        key: getattr(job, key)
        for key in (
            "id",
            "kind",
            "payload",
            "state",
            "attempts",
            "result",
            "message",
            "created_at",
            "updated_at",
            "available_at",
        )
    }


def material_data(material):
    return {
        key: getattr(material, key)
        for key in ("id", "kind", "title", "identifiers", "metadata", "created_at")
    }


def release_data(release):
    return {
        key: getattr(release, key)
        for key in (
            "id",
            "namespace",
            "version",
            "source_url",
            "rights",
            "count",
            "sha256",
            "imported_at",
            "active",
        )
    }


def evidence_data(evidence):
    passage = evidence.passage
    result = {
        key: getattr(evidence, key)
        for key in (
            "id",
            "assertion",
            "quote",
            "context",
            "entity_links",
            "ai_status",
            "human_status",
            "publication_status",
            "published_at",
            "created_at",
            "start",
            "end",
        )
    }
    result["source"] = {
        "material_id": passage.version.material_id,
        "title": passage.version.material.title,
        "version_id": passage.version_id,
        "sha256": passage.version.sha256,
        "rights": passage.version.rights,
        "source_url": passage.version.source_url,
        "passage_id": passage.pk,
        "parser_version": passage.parser_version,
        "integrity_status": passage.version.integrity_status,
        "integrity_note": passage.version.integrity_note,
        "locator": passage.locator,
        "text": passage.text,
    }
    result["reviews"] = [
        {
            "id": r.pk,
            "kind": r.kind,
            "decision": r.decision,
            "reason": r.reason,
            "actor": r.actor.get_username() if r.actor else None,
            "provenance": r.provenance,
            "created_at": r.created_at,
        }
        for r in evidence.reviews.select_related("actor").all()
    ]
    return result


@api(["GET"])
def overview(request):
    beat = WorkerHeartbeat.objects.filter(name="curation").first()
    return JsonResponse(
        {
            "materials": Material.objects.count(),
            "versions": MaterialVersion.objects.count(),
            "evidence": dict(
                Evidence.objects.values("publication_status")
                .annotate(n=Count("id"))
                .values_list("publication_status", "n")
            ),
            "workflows": dict(
                WorkItem.objects.values("state")
                .annotate(n=Count("id"))
                .values_list("state", "n")
            ),
            "sources": [
                release_data(r) for r in SourceRelease.objects.filter(active=True)
            ],
            "worker_online": bool(
                beat and (timezone.now() - beat.last_seen).total_seconds() < 30
            ),
            "model_configured": bool(settings.CURATION_MODEL_BACKEND),
            "publication_policy": settings.CURATION_POLICY_VERSION,
        }
    )


@api(["GET", "POST"])
def sources(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "results": [
                    release_data(r)
                    for r in SourceRelease.objects.order_by("-imported_at")[:50]
                ]
            }
        )
    namespace = field(body(request), "namespace", 24)
    if namespace not in ("MONDO", "RXNORM"):
        raise SourceBlocked("来源必须是 MONDO 或 RXNORM")
    job = enqueue(
        "sync_standard",
        {"namespace": namespace, "check_date": timezone.now().date().isoformat()},
    )
    return JsonResponse(job_data(job), status=202)


@api(["GET"])
def concepts(request):
    namespace = request.GET.get("namespace", "MONDO")
    query = request.GET.get("q", "").strip()[:200]
    offset, limit = page(request)
    release = SourceRelease.objects.filter(namespace=namespace, active=True).first()
    rows = Concept.objects.filter(release=release).order_by("label", "identifier")
    if query:
        rows = rows.filter(
            Q(identifier__iexact=query)
            | Q(label__icontains=query)
            | Q(synonyms__icontains=query)
        )
    return JsonResponse(
        {
            "release": release_data(release) if release else None,
            "total": rows.count(),
            "offset": offset,
            "limit": limit,
            "results": list(
                rows.values(
                    "id",
                    "identifier",
                    "label",
                    "synonyms",
                    "definition",
                    "cross_references",
                    "parents",
                )[offset : offset + limit]
            ),
        }
    )


@api(["GET", "POST"])
def materials(request):
    if request.method == "GET":
        offset, limit = page(request)
        rows = Material.objects.order_by("-created_at")
        query = request.GET.get("q", "").strip()[:200]
        if query:
            rows = rows.filter(
                Q(title__icontains=query) | Q(identifiers__icontains=query)
            )
        counts = {kind: 0 for kind in Material.Kind.values}
        counts.update(
            rows.order_by()
            .values("kind")
            .annotate(n=Count("id"))
            .values_list("kind", "n")
        )
        counts["all"] = sum(counts.values())
        kind = request.GET.get("kind", "")
        if kind:
            if kind not in Material.Kind.values:
                raise SourceBlocked("不支持的资料类型")
            rows = rows.filter(kind=kind)
        return JsonResponse(
            {
                "total": rows.count(),
                "counts": counts,
                "results": [material_data(m) for m in rows[offset : offset + limit]],
            }
        )
    if request.content_type != "multipart/form-data":
        raise SourceBlocked("请通过上传表单提交 PDF")
    upload = request.FILES.get("file")
    if not upload or upload.size > 20 * 1024 * 1024:
        raise SourceBlocked("请选择不超过 20 MB 的 PDF")
    title = field(request.POST, "title", 2000)
    kind = field(request.POST, "kind", 20)
    if kind not in Material.Kind.values:
        raise SourceBlocked("不支持的资料类型")
    if request.POST.get("rights_confirmed") != "true":
        raise SourceBlocked("请确认你拥有合法使用本文件的权利")
    rights = field(request.POST, "rights", 4000)
    start_parse = request.POST.get("start_parse", "true")
    if start_parse not in ("true", "false"):
        raise SourceBlocked("解析选项必须为 true 或 false")
    material_id = request.POST.get("material_id") or None
    if material_id:
        get_object_or_404(Material, pk=material_id)
    material, version, job = upload_pdf(
        upload.read(),
        title,
        kind,
        rights,
        material_id,
        filename=upload.name,
        start_parse=start_parse == "true",
    )
    return JsonResponse(
        {
            "material": material_data(material),
            "version_id": version.pk,
            "job": job_data(job) if job else None,
        },
        status=201,
    )


@api(["GET"])
def material_detail(request, pk):
    material = get_object_or_404(Material, pk=pk)
    versions = list(material.versions.order_by("-acquired_at"))
    selected = request.GET.get("version")
    version = (
        next((v for v in versions if str(v.pk) == selected), None)
        if selected
        else next(
            (
                v
                for v in versions
                if v.media_type != "application/pubmed+xml"
                and v.integrity_status == "valid"
            ),
            next((v for v in versions if v.integrity_status == "valid"), None),
        )
    )
    offset, limit = page(request)
    passages = (
        version.passages.order_by("parser_version", "ordinal") if version else None
    )
    parse_job = (
        WorkItem.objects.filter(kind="parse", payload__version_id=str(version.pk))
        .order_by("-created_at", "-pk")
        .first()
        if version
        else None
    )
    return JsonResponse(
        {
            **material_data(material),
            "versions": [
                {
                    key: getattr(v, key)
                    for key in (
                        "id",
                        "sha256",
                        "media_type",
                        "filename",
                        "source_url",
                        "rights",
                        "acquired_at",
                        "integrity_status",
                        "integrity_note",
                    )
                }
                for v in versions
            ],
            "selected_version": version.pk if version else None,
            "parse_job": job_data(parse_job) if parse_job else None,
            "passage_total": passages.count() if passages is not None else 0,
            "passages": list(
                passages.values("id", "parser_version", "ordinal", "locator", "text")[
                    offset : offset + limit
                ]
            )
            if passages is not None
            else [],
        }
    )


@api(["GET"])
def version_processing(request, pk):
    """Read-only projection of persisted jobs; viewing never creates a parse task."""
    version = get_object_or_404(MaterialVersion, pk=pk)
    offset, limit = page(request)
    rows = WorkItem.objects.filter(
        payload__version_id=str(version.pk), kind__in=("parse", "extract")
    ).order_by("-created_at", "-pk")
    stages = {}
    for kind in ("parse", "extract"):
        job = rows.filter(kind=kind).first()
        stages[kind] = job_data(job) if job else None
    return JsonResponse(
        {
            "version_id": version.pk,
            "archived_at": version.acquired_at,
            "stages": stages,
            "total": rows.count(),
            "results": [job_data(job) for job in rows[offset : offset + limit]],
        }
    )


@api(["GET"])
def download(request, pk):
    version = get_object_or_404(MaterialVersion, pk=pk)
    return FileResponse(
        blob_path(version.sha256).open("rb"),
        as_attachment=True,
        filename=Path(version.filename).name,
        content_type=version.media_type,
    )


@api(["POST"])
def pubmed(request, action):
    data = body(request)
    if action == "search":
        payload = {
            "query": field(data, "query", 500),
            "check_date": timezone.now().date().isoformat(),
        }
        kind = "pubmed_search"
    elif action == "import":
        pmid = field(data, "pmid", 10)
        if not pmid.isdecimal():
            raise SourceBlocked("PMID 只能包含数字")
        payload, kind = (
            {"pmid": pmid, "source_parser_version": "pubmed-id-v2"},
            "acquire_pubmed",
        )
    else:
        raise SourceBlocked("不支持的操作")
    return JsonResponse(job_data(enqueue(kind, payload)), status=202)


@api(["GET"])
def workflows(request):
    offset, limit = page(request)
    rows = WorkItem.objects.all()
    if request.GET.get("state"):
        rows = rows.filter(state=request.GET["state"])
    return JsonResponse(
        {
            "total": rows.count(),
            "results": [job_data(j) for j in rows[offset : offset + limit]],
        }
    )


@api(["GET", "POST"])
def workflow_detail(request, pk):
    job = get_object_or_404(WorkItem, pk=pk)
    if request.method == "POST":
        if field(body(request), "action", 20) != "retry":
            raise SourceBlocked("不支持的操作")
        if not retry_job(job):
            raise SourceBlocked("仅失败、待处理或待配置任务可以重试")
        job.refresh_from_db()
    return JsonResponse(
        {
            **job_data(job),
            "runs": list(
                job.runs.order_by("number").values(
                    "number", "started_at", "finished_at", "outcome", "message"
                )
            ),
        }
    )


@api(["GET"])
def evidence(request):
    offset, limit = page(request)
    rows = Evidence.objects.select_related(
        "passage__version__material"
    ).prefetch_related("reviews__actor")
    status = request.GET.get("status", "published")
    if status != "all":
        rows = rows.filter(publication_status=status)
        if status == "published":
            rows = rows.filter(passage__version__integrity_status="valid")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        rows = rows.filter(
            Q(assertion__icontains=query) | Q(entity_links__icontains=query)
        )
    return JsonResponse(
        {
            "total": rows.count(),
            "results": [evidence_data(e) for e in rows[offset : offset + limit]],
            "policy": settings.CURATION_POLICY_VERSION,
        }
    )


@api(["GET", "POST"])
def evidence_detail(request, pk):
    candidate = get_object_or_404(Evidence, pk=pk)
    if request.method == "POST":
        data = body(request)
        candidate = human_review(
            pk, request.user, field(data, "decision", 16), field(data, "reason", 10000)
        )
    return JsonResponse(evidence_data(candidate))


@ensure_csrf_cookie
@api(["GET", "POST", "DELETE"])
def session(request):
    if request.method == "POST":
        # A small per-address limit protects the LAN sign-in endpoint, without persisting passwords.
        address = request.META.get("REMOTE_ADDR", "unknown")
        key = f"curation-login:{address}"
        if cache.get(key, 0) >= 5:
            return JsonResponse(
                {"error": "登录尝试过多，请 5 分钟后重试。"}, status=429
            )
        data = body(request)
        user = authenticate(
            request,
            username=field(data, "username", 150),
            password=field(data, "password", 1024, strip=False),
        )
        if user is None:
            cache.set(key, cache.get(key, 0) + 1, 300)
            return JsonResponse({"error": "账号或密码不正确。"}, status=401)
        login(request, user)
        request.session.set_expiry(8 * 60 * 60)
        cache.delete(key)
    elif request.method == "DELETE":
        logout(request)
    return JsonResponse(
        {
            "username": request.user.get_username()
            if request.user.is_authenticated
            else None,
            "can_review": request.user.is_authenticated
            and request.user.has_perm("curation.change_evidence"),
        }
    )
