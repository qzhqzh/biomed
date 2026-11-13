import hashlib
import json
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from apps.curation.materials import upload_pdf
from apps.curation.models import (
    Concept,
    EntityTranslation,
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
from apps.curation.worker_heartbeat import worker_last_seen
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


@api(["GET", "POST"])
def collections(request):
    from apps.curation.collections import collection_data, create_collection
    from apps.curation.models import LiteratureCollection

    if request.method == "GET":
        rows = LiteratureCollection.objects.order_by("-created_at")[:20]
        return JsonResponse(
            {
                "results": [
                    collection_data(
                        row, summary=request.GET.get("summary") == "1", cached=True
                    )
                    for row in rows
                ]
            }
        )
    data = body(request)
    run = create_collection(
        field(data, "start_date", 10),
        field(data, "end_date", 10),
        data.get("limits", {}),
    )
    return JsonResponse(collection_data(run), status=201)


@api(["GET", "POST"])
def collection_detail(request, pk):
    from apps.curation.collections import collection_data, control_collection
    from apps.curation.models import LiteratureCollection

    run = get_object_or_404(LiteratureCollection, pk=pk)
    if request.method == "POST":
        data = body(request)
        run = control_collection(pk, field(data, "action", 12), data.get("limits", {}))
    return JsonResponse(
        collection_data(
            run,
            detail=True,
            classification_sha=request.GET.get("classification_sha"),
            summary=request.method == "GET" and request.GET.get("summary") == "1",
            cached=request.method == "GET",
        )
    )


@api(["GET"])
def collection_alerts(request):
    from apps.curation.collection_notifications import MailNotConfigured, mail_config
    from apps.curation.collection_recovery import COMPLETED_REASON
    from apps.curation.models import CollectionAlert, LiteratureCollection

    rows = (
        CollectionAlert.objects.filter(resolved_at__isnull=True)
        .exclude(reason=COMPLETED_REASON)
        .order_by("-created_at")
    )
    try:
        mail_config()
        configured = True
    except MailNotConfigured:
        configured = False
    seen = worker_last_seen(WorkerHeartbeat)
    active = LiteratureCollection.objects.filter(
        state__in=("running", "estimating")
    ).exists()
    return JsonResponse(
        {
            "count": rows.count(),
            "results": list(
                rows.values(
                    "id",
                    "collection_id",
                    "reason",
                    "message",
                    "created_at",
                    "email_state",
                    "email_attempts",
                    "email_sent_at",
                    "email_error",
                )[:5]
            ),
            "mail_configured": configured,
            "worker_stalled": bool(
                active
                and (not seen or (timezone.now() - seen).total_seconds() > 90)
            ),
        }
    )


@api(["GET"])
def literature_taxonomy(request):
    from apps.curation.classification import DEFAULT_TAXONOMY, taxonomy_snapshot

    return JsonResponse(taxonomy_snapshot(request.GET.get("version", DEFAULT_TAXONOMY)))


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
    seen = worker_last_seen(WorkerHeartbeat)
    runtime = {
        "worker_online": bool(
            seen and (timezone.now() - seen).total_seconds() < 30
        ),
        "model_configured": bool(settings.CURATION_MODEL_BACKEND),
        "publication_policy": settings.CURATION_POLICY_VERSION,
    }
    if request.GET.get("runtime") == "1":
        return JsonResponse(runtime)
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
            **runtime,
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
    from apps.curation.standards import SOURCES

    if namespace not in SOURCES:
        raise SourceBlocked("不支持的数据来源")
    job = enqueue(
        "sync_standard",
        {"namespace": namespace, "check_date": timezone.now().date().isoformat()},
    )
    return JsonResponse(job_data(job), status=202)


@api(["GET"])
def concepts(request):
    namespace = request.GET.get("namespace", "MONDO")
    query = request.GET.get("q", "").strip()[:200]
    path_identifier = request.GET.get("tree_path")
    if path_identifier is not None and namespace != "ONCOTREE":
        raise SourceBlocked("树路径只适用于 ONCOTREE 癌种分类")
    offset, limit = page(request)
    release = SourceRelease.objects.filter(namespace=namespace, active=True).first()
    # Pin subsequent branch navigation to the displayed immutable release.
    if request.GET.get("release"):
        release = get_object_or_404(
            SourceRelease, pk=request.GET["release"], namespace=namespace
        )
    rows = Concept.objects.filter(release=release).order_by("label", "identifier")
    navigation = None
    parent = request.GET.get("parent")
    if parent is not None and release:
        from apps.curation.concept_hierarchy import branch

        identifiers, navigation = branch(release.pk, parent)
        rows = rows.filter(identifier__in=identifiers)
    if query:
        translated = EntityTranslation.objects.filter(
            concept_id=OuterRef("pk"), label__icontains=query
        )
        rows = rows.annotate(translated_match=Exists(translated)).filter(
            Q(identifier__iexact=query)
            | Q(label__icontains=query)
            | Q(synonyms__icontains=query)
            | Q(translated_match=True)
        )
    from apps.curation.reference_resources import add_translations

    if navigation:
        nodes = ([navigation["focus"]] if navigation["focus"] else []) + navigation[
            "parents"
        ]
        translated_nodes = add_translations(
            list(
                Concept.objects.filter(
                    release=release,
                    identifier__in=[node["identifier"] for node in nodes],
                ).values("id", "identifier")
            )
        )
        by_identifier = {
            row["identifier"]: row["translations"] for row in translated_nodes
        }
        for node in nodes:
            node["translations"] = by_identifier.get(node["identifier"], [])

    results = add_translations(
        list(
            rows.values(
                "id",
                "identifier",
                "label",
                "synonyms",
                "definition",
                "cross_references",
                "parents",
            )[offset : offset + limit]
        )
    )
    if namespace == "ONCOTREE" and release:
        from apps.curation.concept_hierarchy import release_graph

        _, children, _ = release_graph(str(release.pk))
        for result in results:
            result["child_count"] = len(children[result["identifier"]])
    data = {
        "release": release_data(release) if release else None,
        "total": rows.count(),
        "offset": offset,
        "limit": limit,
        "navigation": navigation,
        "results": results,
    }
    if path_identifier is not None:
        from apps.curation.concept_hierarchy import tree_path

        if not release:
            raise SourceBlocked("暂无可用的 ONCOTREE 癌种分类版本")
        data["tree_path"] = tree_path(release.pk, path_identifier)
    return JsonResponse(data)


@api(["GET"])
def drug_resources(request):
    from apps.curation.reference_resources import drug_detail

    release = SourceRelease.objects.filter(namespace="RXNORM", active=True).first()
    if request.GET.get("release"):
        release = get_object_or_404(
            SourceRelease, pk=request.GET["release"], namespace="RXNORM"
        )
    concept = get_object_or_404(
        Concept, release=release, identifier=request.GET.get("identifier", "")
    )
    return JsonResponse(drug_detail(concept))


@api(["GET", "POST"])
def materials(request):
    if request.method == "GET":
        offset, limit = page(request)
        summary = request.GET.get("summary") == "1"
        rows = Material.objects.order_by("-created_at")
        query = request.GET.get("q", "").strip()[:200]
        if query:
            rows = rows.filter(
                Q(title__icontains=query) | Q(identifiers__icontains=query)
            )
        kind = request.GET.get("kind", "")
        if kind and kind not in Material.Kind.values:
            raise SourceBlocked("不支持的资料类型")
        # User uploads must become visible immediately across Gunicorn workers.
        # This bounded range uses the source_key unique index (including COUNT),
        # so it reads only upload keys, not article metadata or the whole library.
        # PubMed collection does not change this generation or evict cached pages.
        upload_generation = (
            Material.objects.filter(
                source_key__gte="upload:", source_key__lt="upload;"
            ).count()
            if summary
            else 0
        )
        cache_key = (
            f"curation:material-counts:v2:{upload_generation}:"
            + hashlib.sha256(query.encode()).hexdigest()
        )
        count_snapshot = cache.get(cache_key) if summary else None
        if count_snapshot is None:
            # Fixed categories need no GROUP BY sort. The compact listing index
            # covers kind/id so normal listing counts never read document JSON.
            counts = rows.order_by().aggregate(
                **{
                    name: Count("id", filter=Q(kind=name))
                    for name in Material.Kind.values
                },
                all=Count("id"),
            )
            count_snapshot = {"counts": counts, "as_of": timezone.now().isoformat()}
            if summary:
                cache.set(cache_key, count_snapshot, 60)
        counts = count_snapshot["counts"]
        if kind:
            rows = rows.filter(kind=kind)
        # Sort only IDs before reading large metadata. SQLite otherwise carries
        # every candidate's JSON through the unindexed sort for a small page.
        page_key = f"{cache_key}:page:{kind}:{offset}:{limit}"
        selected = cache.get(page_key) if summary else None
        if selected is None:
            selected = {
                "ids": list(rows.values_list("pk", flat=True)[offset : offset + limit]),
                "as_of": timezone.now().isoformat(),
            }
            if summary:
                cache.set(page_key, selected, 60)
        page_rows = Material.objects.filter(pk__in=selected["ids"])
        if summary:
            page_rows = page_rows.only(
                "id", "kind", "title", "identifiers", "created_at"
            )
        by_id = {row.pk: row for row in page_rows}
        items = [
            {
                name: getattr(by_id[pk], name)
                for name in ("id", "kind", "title", "identifiers", "created_at")
            }
            if summary
            else material_data(by_id[pk])
            for pk in selected["ids"]
            if pk in by_id
        ]
        return JsonResponse(
            {
                "total": counts[kind or "all"],
                "counts": counts,
                "counts_as_of": count_snapshot["as_of"],
                "results_as_of": selected["as_of"],
                "results": items,
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
