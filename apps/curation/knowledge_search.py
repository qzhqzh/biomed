"""Search only current, approved relationship links; preserve source quotations."""

import json

from django.core.exceptions import PermissionDenied
from django.db.models import Exists, F, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.curation.entity_registry_integration import registry_match_query
from apps.curation.knowledge_eligibility import assess_record
from apps.curation.knowledge_presentation import evidence_presentation
from apps.curation.knowledge_publication import ROLES, human_review, normalize_term
from apps.curation.knowledge_views import _page, _response, article_data
from apps.curation.models import KnowledgeCandidate, KnowledgeSearchTerm


def published_candidates():
    return (
        KnowledgeCandidate.objects.select_related("version__material", "current_review")
        .filter(
            publication_status="published",
            ai_status="pass",
            version__integrity_status="valid",
            current_review__kind="ai",
            current_review__decision="pass",
            current_review__candidate_id=F("id"),
        )
        .exclude(human_status="flagged")
    )


def review_data(review):
    return {
        "id": str(review.pk),
        "kind": review.kind,
        "decision": review.decision,
        "reason": review.reason,
        "provider": review.provider,
        "model": review.model,
        "policy_version": review.policy_version,
        "review_run": review.review_run,
        "created_at": review.created_at.isoformat(),
        "input_sha256": review.input_sha256,
        "actor": review.actor.get_username() if review.actor_id else None,
    }


def knowledge_data(candidate, *, filters=None, history=False):
    review = candidate.current_review
    result = review.result if review else {}
    passages = (
        {p["id"]: p for p in review.input_snapshot.get("passages", [])}
        if review
        else {}
    )
    aliases_by_passage = {}
    for anchor in review.input_snapshot.get("anchors", []) if review else []:
        alias = (anchor.get("attributes") or {}).get("alias")
        if alias and anchor.get("passage_id"):
            aliases_by_passage.setdefault(anchor["passage_id"], set()).add(alias)

    def quoted(raw):
        return {**raw, "passage": passages.get(raw["passage_id"]),
                "aliases": sorted(aliases_by_passage.get(raw["passage_id"], []))}

    links = [
        {**link, "support": [quoted(q) for q in link["support"]]}
        for link in result.get("approved_links", [])
    ]
    filters = filters or {}
    matches = []
    for link in links:
        for alias in link["aliases"]:
            if any(
                (role == "q" or role == link["role"])
                and term == normalize_term(alias["term"])
                for role, term in filters.items()
            ):
                matches.append(
                    {"entity_id": link["entity_id"], "role": link["role"], **alias}
                )
    article = article_data(candidate.version)
    data = {
        "id": str(candidate.pk),
        "source_run": candidate.source_run,
        "group_id": candidate.group_id,
        "status": candidate.status,
        "ai_status": candidate.ai_status,
        "human_status": candidate.human_status,
        "publication_status": candidate.publication_status,
        "published_at": candidate.published_at.isoformat()
        if candidate.published_at
        else None,
        "article": article,
        "fact": candidate.fact,
        "evidence_presentation": evidence_presentation(candidate.fact,
            [quoted(q) for q in result.get("supporting_quotes", [])], version_id=candidate.version_id,
            pmid=article.get("pmid")),
        "eligibility": assess_record(
            {"fact": candidate.fact, "assessment": candidate.assessment}
        ),
        "review": review_data(review) if review else None,
        "approved_links": links,
        "matched_terms": matches,
        "supporting_quotes": [quoted(q) for q in result.get("supporting_quotes", [])],
    }
    if history:
        data["review_history"] = [
            {**review_data(item), "result": item.result}
            for item in candidate.reviews.select_related("actor")
        ]
    return data


@require_GET
def knowledge(request, pk=None):
    if pk is not None:
        candidate = get_object_or_404(
            KnowledgeCandidate.objects.select_related(
                "version__material", "current_review__actor"
            ),
            pk=pk,
        )
        return _response(knowledge_data(candidate, history=True))
    queryset = published_candidates()
    filters = {}
    for role in sorted(ROLES | {"q"}):
        value = request.GET.get(role, "").strip()
        if not value:
            continue
        if len(value) > 2000:
            return _response({"error": "检索词不能超过 2000 字符"}, status=400)
        normalized = normalize_term(value)
        filters[role] = normalized
        terms = KnowledgeSearchTerm.objects.filter(
            candidate_id=OuterRef("pk"),
            review_id=OuterRef("current_review_id"),
            normalized_term=normalized,
        )
        if role != "q":
            terms = terms.filter(role=role)
        condition = Q(Exists(terms))
        if role in {"drug", "disease", "q"}:
            condition |= Q(
                Exists(
                    registry_match_query(
                        value, kind=None if role == "q" else role, published=True
                    )
                )
            )
        queryset = queryset.filter(condition)
    if source_run := request.GET.get("source_run"):
        queryset = queryset.filter(source_run=source_run)
    return _page(
        request, queryset, lambda candidate: knowledge_data(candidate, filters=filters)
    )


@require_POST
def review_knowledge(request, pk):
    if not request.user.is_authenticated:
        return _response({"error": "请先登录后进行抽查"}, status=401)
    if not request.user.has_perm("curation.change_evidence"):
        return _response({"error": "当前账号没有证据抽查权限"}, status=403)
    get_object_or_404(KnowledgeCandidate, pk=pk)
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise TypeError("请求内容必须是 JSON 对象")
        human_review(pk, request.user, body.get("decision"), body.get("reason"))
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        return _response({"error": str(error)}, status=400)
    except PermissionDenied as error:
        return _response({"error": str(error)}, status=403)
    candidate = KnowledgeCandidate.objects.select_related(
        "version__material", "current_review__actor"
    ).get(pk=pk)
    return _response(knowledge_data(candidate, history=True))
