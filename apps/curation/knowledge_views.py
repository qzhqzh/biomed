"""Read-only views of source-scoped research knowledge, never published Evidence."""

import re

from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.curation.entity_registry_integration import (
    normalized_participant_data,
    registry_match_query,
)
from apps.curation.entity_registry_models import EntityRegistryMention
from apps.curation.knowledge_eligibility import assess_record
from apps.curation.knowledge_presentation import evidence_presentation
from apps.curation.knowledge_target_readiness import target_readiness
from apps.curation.models import (
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    KnowledgeReview,
)

# These are explicit roles in one extracted fact, not types inferred from every
# mention in its source article. Comparator/context mentions never match a drug.
RELATION_FILTERS = {
    "disease": ("disease", {"disease"}),
    "drug": ("intervention", {"drug", "treatment"}),
    "biomarker": ("molecular", {"gene", "protein", "variant", "biomarker"}),
}
RELATION_ASSOCIATIONS = {"model_selected", "exact_fact_text"}
EXTRACTION_ROLES = {"disease", "intervention", "comparator", "molecular", "context"}

PRIVATE_KEYS = re.compile(
    r"password|secret|credential|cookie|token|(?:^|_)paths?$|private", re.IGNORECASE
)
LOCAL_PATH = re.compile(
    r"(?:^|\s)(?:/(?:appdata|data|home|tmp|mnt|root)/\S+|file://\S+)"
)


def public_json(value):
    """Research metadata may retain local audit paths; the public API must not."""
    if isinstance(value, dict):
        return {
            key: public_json(item)
            for key, item in value.items()
            if not PRIVATE_KEYS.search(key) and not key.startswith("/")
        }
    if isinstance(value, list):
        return [public_json(item) for item in value]
    if isinstance(value, str):
        return LOCAL_PATH.sub(" [local artifact]", value)
    return value


def _response(value, *, status=200):
    response = JsonResponse(value, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _page(request, queryset, serialize, *, count_by_pk=False, prepare=None):
    try:
        limit = int(request.GET.get("limit", 30))
        offset = int(request.GET.get("offset", 0))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
    except ValueError:
        return _response(
            {"error": "limit 须为 1–100，offset 须为非负整数。"}, status=400
        )
    # Count candidate identities rather than grouping all large JSON fields and
    # all anchors. Filters that join anchors can still produce duplicate rows.
    count = (
        queryset.order_by().values("pk").distinct().count()
        if count_by_pk else queryset.count()
    )
    items = list(queryset[offset : offset + limit])
    if prepare is not None:
        prepare(items)
    return _response(
        {
            "count": count,
            "limit": limit,
            "offset": offset,
            "results": [serialize(item) for item in items],
        }
    )


def article_data(version):
    material = version.material
    identifiers = material.identifiers
    return {
        "material_id": str(material.pk),
        "version_id": str(version.pk),
        "title": material.title,
        "pmid": identifiers.get("pubmed") or identifiers.get("pmid"),
        "pmcid": identifiers.get("pmc") or identifiers.get("pmcid"),
        "doi": identifiers.get("doi"),
        "sha256": version.sha256,
        "source_url": public_json(version.source_url),
        "rights": version.rights,
        "integrity_status": version.integrity_status,
    }


def entity_data(entity):
    return {
        "id": str(entity.pk),
        "kind": entity.kind,
        "subtype": entity.subtype,
        "label": entity.label,
        "status": entity.status,
        "scope_version_id": str(entity.scope_version_id),
        "attributes": public_json(entity.attributes),
        "candidate_count": getattr(entity, "candidate_count", 0),
    }


def candidate_data(candidate):
    def stage(name, default):
        value = candidate.assessment.get(name)
        return public_json(value) if isinstance(value, dict) else {"status": default}

    quality = stage("quality", "unreviewed")
    if not isinstance(candidate.assessment.get("quality"), dict) and candidate.current_review_id:
        previous = candidate.current_review
        quality = {
            "status": f"legacy_{previous.decision}",
            "reason": previous.reason,
            "review_id": str(previous.pk),
            "model": previous.model,
            "policy_version": previous.policy_version,
            "origin": "publication_review",
        }
    reviews = getattr(candidate, "quality_reviews", [])
    if reviews:
        latest = reviews[0]
        quality = {
            "status": {"pass": "valid", "fail": "invalid"}.get(latest.decision, "undetermined"),
            "reason": latest.reason,
            "review_id": str(latest.pk),
            "model": latest.model,
            "policy_version": latest.policy_version,
            "created_at": latest.created_at.isoformat(),
            **{
                key: latest.result[key]
                for key in ("schema_version", "quality_dimensions", "issues")
                if key in latest.result
            },
        }
    participants = {}
    for anchor in getattr(candidate, "relationship_anchors", []):
        for field, (role, kinds) in RELATION_FILTERS.items():
            if role not in anchor.roles or anchor.entity.kind not in kinds:
                continue
            key = (str(anchor.entity_id), field)
            if key not in participants:
                participants[key] = {
                    "entity_id": str(anchor.entity_id),
                    "entity_ref": anchor.entity_ref,
                    "label": anchor.entity.label,
                    "role": field,
                    "anchor_ids": [],
                }
            participants[key]["anchor_ids"].append(str(anchor.pk))
    readiness = target_readiness({
        "fact": candidate.fact, "assessment": candidate.assessment,
        "entities": [{"label": a.entity.label} for a in getattr(candidate, "relationship_anchors", [])],
        "anchors": [{"purpose": a.purpose, "quote": a.quote, "start": a.start, "end": a.end}
                    for a in getattr(candidate, "target_evidence_anchors", [])],
    })
    compatibility = stage("compatibility", "unassessed")
    if readiness["status"] == "outside_target" and compatibility["status"] != "invalid_source":
        compatibility = {**compatibility, "original_status": compatibility["status"],
                         "status": "not_representable", "reason": readiness["reason"],
                         "diagnostic_version": readiness["version"]}
    article = article_data(candidate.version)
    sources = []
    aliases = candidate.fact.get("evidence") if isinstance(candidate.fact, dict) else None
    for anchor in getattr(candidate, "target_evidence_anchors", []):
        if aliases is not None and (not isinstance(aliases, list)
                                    or (anchor.attributes or {}).get("alias") not in aliases):
            continue
        passage = anchor.passage
        sources.append({"passage_id": str(passage.pk), "start": anchor.start, "end": anchor.end,
                        "quote": anchor.quote, "aliases": [(anchor.attributes or {}).get("alias")],
                        "passage": {"id": str(passage.pk), "version_id": str(passage.version_id),
                                                          "locator": passage.locator, "text": passage.text}})
    return {
        "id": str(candidate.pk),
        "source_run": candidate.source_run,
        "group_id": candidate.group_id,
        "status": candidate.status,
        "ai_status": candidate.ai_status,
        "human_status": candidate.human_status,
        "publication_status": candidate.publication_status,
        "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
        "current_review_id": str(candidate.current_review_id) if candidate.current_review_id else None,
        "article": article,
        "assessment": public_json(candidate.assessment),
        "execution": stage("execution", "stored"),
        "compatibility": compatibility,
        "target_readiness": readiness,
        "quality": public_json(quality),
        "evidence_presentation": evidence_presentation(candidate.fact, sources,
            version_id=candidate.version_id, pmid=article.get("pmid")),
        "eligibility": assess_record({"fact": candidate.fact, "assessment": candidate.assessment}),
        "relationship_participants": list(participants.values()),
        "normalized_participants": public_json([normalized_participant_data(m) for m in getattr(candidate, "registry_participants", [])]),
        "fact_summary": public_json({
            key: candidate.fact[key]
            for key in ("relation", "population", "treatment", "conclusion", "direction")
            if key in candidate.fact
        }),
        "entity_count": getattr(candidate, "entity_count", 0),
        "anchor_count": getattr(candidate, "anchor_count", 0),
    }


def anchor_data(anchor):
    passage = anchor.passage
    return {
        "id": str(anchor.pk),
        "candidate_id": str(anchor.candidate_id),
        "entity_id": str(anchor.entity_id) if anchor.entity_id else None,
        "entity_ref": anchor.entity_ref,
        "purpose": anchor.purpose,
        "association": anchor.association,
        "roles": anchor.roles,
        "start": anchor.start,
        "end": anchor.end,
        "quote": anchor.quote,
        "attributes": public_json(anchor.attributes),
        "passage": {
            "id": str(passage.pk),
            "locator": passage.locator,
            "parser_version": passage.parser_version,
            "text": passage.text,
        },
    }


def _candidates():
    return (
        KnowledgeCandidate.objects.select_related("version__material", "current_review")
        .prefetch_related(
            Prefetch("anchors", queryset=KnowledgeAnchor.objects.filter(purpose="evidence").select_related("passage")
                     .only("candidate_id", "purpose", "quote", "start", "end", "attributes", "passage_id",
                           "passage__id", "passage__version_id", "passage__locator", "passage__text"),
                     to_attr="target_evidence_anchors"),
            Prefetch(
                "registry_mentions",
                queryset=EntityRegistryMention.objects.select_related("canonical_entity__main_cancer", "applied_rule"),
                to_attr="registry_participants",
            ),
            Prefetch(
                "anchors",
                queryset=KnowledgeAnchor.objects.filter(
                    purpose="mention",
                    association__in=RELATION_ASSOCIATIONS,
                    entity__isnull=False,
                ).select_related("entity"),
                to_attr="relationship_anchors",
            ),
            Prefetch(
                "reviews",
                queryset=KnowledgeReview.objects.filter(kind="quality").order_by("-created_at", "-id"),
                to_attr="quality_reviews",
            ),
        )
    )


def _attach_candidate_counts(candidates):
    """Aggregate anchors for the requested page only, preserving API counts."""
    if not candidates:
        return
    counts = {
        row["candidate_id"]: row
        for row in KnowledgeAnchor.objects.filter(candidate_id__in=[c.pk for c in candidates])
        .order_by()
        .values("candidate_id")
        .annotate(entity_count=Count("entity_id", distinct=True), anchor_count=Count("id"))
    }
    for candidate in candidates:
        row = counts.get(candidate.pk, {})
        candidate.entity_count = row.get("entity_count", 0)
        candidate.anchor_count = row.get("anchor_count", 0)


def _entities():
    return KnowledgeEntity.objects.annotate(
        candidate_count=Count("anchors__candidate", distinct=True)
    )


@require_GET
def entities(request, pk=None):
    if pk is not None:
        entity = get_object_or_404(
            _entities().select_related("scope_version__material"), pk=pk
        )
        result = entity_data(entity)
        candidates = _candidates().filter(anchors__entity_id=pk).distinct()
        anchors = KnowledgeAnchor.objects.filter(entity_id=pk).select_related("passage")
        candidate_count = candidates.order_by().values("pk").distinct().count()
        anchor_count = anchors.count()
        candidate_items = list(candidates[:100])
        _attach_candidate_counts(candidate_items)
        result.update(
            {
                "article": article_data(entity.scope_version),
                "candidates": [candidate_data(item) for item in candidate_items],
                "anchors": [anchor_data(item) for item in anchors[:100]],
                "candidate_count": candidate_count,
                "anchor_count": anchor_count,
                "candidates_truncated": candidate_count > 100,
                "anchors_truncated": anchor_count > 100,
            }
        )
        return _response(result)
    queryset = _entities()
    if kind := request.GET.get("kind"):
        queryset = queryset.filter(kind=kind)
    if query := request.GET.get("q", "").strip():
        queryset = queryset.filter(label__icontains=query[:200])
    if source_run := request.GET.get("source_run"):
        queryset = queryset.filter(anchors__candidate__source_run=source_run)
    if group_id := request.GET.get("group_id"):
        queryset = queryset.filter(anchors__candidate__group_id=group_id)
    return _page(request, queryset.distinct(), entity_data)


@require_GET
def candidates(request, pk=None):
    if pk is not None:
        candidate = get_object_or_404(_candidates(), pk=pk)
        _attach_candidate_counts([candidate])
        result = candidate_data(candidate)
        result.update(
            {
                "fact": public_json(candidate.fact),
                "provenance": public_json(candidate.provenance),
                "entities": [
                    entity_data(entity)
                    for entity in _entities()
                    .filter(anchors__candidate_id=pk)
                    .distinct()
                ],
                "anchors": [
                    anchor_data(anchor)
                    for anchor in candidate.anchors.select_related("passage")
                ],
            }
        )
        return _response(result)
    queryset = _candidates()
    for field, (role, kinds) in RELATION_FILTERS.items():
        term = request.GET.get(field, "").strip()
        if not term:
            continue
        if len(term) > 2000:
            return _response({"error": "检索词不能超过 2000 字符"}, status=400)
        # Fixed role vocabulary has at most five distinct entries. JSON index
        # lookup works on both SQLite and PostgreSQL; JSON contains does not.
        role_query = Q()
        for index in range(len(EXTRACTION_ROLES)):
            role_query |= Q(**{f"roles__{index}": role})
        anchors = KnowledgeAnchor.objects.filter(
            role_query,
            candidate_id=OuterRef("pk"),
            purpose="mention",
            association__in=RELATION_ASSOCIATIONS,
            entity__kind__in=kinds,
            entity__label__icontains=term,
        )
        condition = Q(Exists(anchors))
        if field in {"drug", "disease"}:
            condition |= Q(Exists(registry_match_query(term, kind=field, contains=True)))
        queryset = queryset.filter(condition)
    if source_run := request.GET.get("source_run"):
        queryset = queryset.filter(source_run=source_run)
    if group_id := request.GET.get("group_id"):
        queryset = queryset.filter(group_id=group_id)
    if kind := request.GET.get("kind"):
        queryset = queryset.filter(anchors__entity__kind=kind)
    if query := request.GET.get("q", "").strip():
        query = query[:200]
        original_mentions = KnowledgeAnchor.objects.filter(
            candidate_id=OuterRef("pk"), entity__label__icontains=query
        )
        queryset = queryset.filter(
            Q(group_id__icontains=query)
            | Q(version__material__title__icontains=query)
            | Q(version__material__identifiers__pubmed__icontains=query)
            | Q(version__material__identifiers__pmid__icontains=query)
            | Q(Exists(original_mentions))
            | Q(Exists(registry_match_query(query, contains=True)))
        )
    return _page(
        request, queryset.distinct(), candidate_data,
        count_by_pk=True, prepare=_attach_candidate_counts,
    )
