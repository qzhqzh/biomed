"""Local registry browse and authenticated, versioned review endpoints."""

import json
from collections import Counter

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import OperationalError
from django.db.models import Count, Exists, OuterRef, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.curation import entity_registry as service
from apps.curation.entity_registry_models import (
    CanonicalEntity,
    EntityRegistryDecision,
    EntityRegistryImport,
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import KnowledgeAnchor
from apps.curation.knowledge_views import article_data, public_json


def response(value, status=200):
    reply = JsonResponse(public_json(value), status=status)
    reply["Cache-Control"] = "no-store"
    return reply


def can_review(request):
    return request.user.is_authenticated and request.user.has_perm(
        "curation.change_evidence"
    )


def entity_queryset():
    return CanonicalEntity.objects.select_related("main_cancer").annotate(
        mention_count=Count("mentions", distinct=True),
        alias_count=Count("rules", distinct=True),
    )


def entity_data(entity):
    return {
        "state_sha256": service.digest(service.entity_snapshot(entity)),
        **service.entity_snapshot(entity),
        "main_cancer": {
            "id": str(entity.main_cancer_id),
            "name": entity.main_cancer.name,
        }
        if entity.main_cancer_id
        else None,
        "mention_count": entity.mention_count
        if hasattr(entity, "mention_count")
        else entity.mentions.count(),
        "alias_count": entity.alias_count
        if hasattr(entity, "alias_count")
        else entity.rules.count(),
    }


def matching_mentions(rule):
    query = EntityRegistryMention.objects.filter(
        kind=rule.kind, normalized_label=rule.normalized_alias
    )
    if rule.scope_type == "article":
        query = query.filter(scope_key=rule.scope_key)
    return query


def rule_data(rule):
    return {
        "state_sha256": service.digest(service.rule_snapshot(rule)),
        **service.rule_snapshot(rule),
        "canonical_entity": entity_data(rule.canonical_entity)
        if rule.canonical_entity_id
        else None,
        "mention_count": matching_mentions(rule).count(),
    }


def mention_data(mention):
    candidate = mention.candidate
    anchors = KnowledgeAnchor.objects.filter(
        candidate=candidate, entity_ref=mention.entity_ref
    ).select_related("passage")
    return {
        "id": str(mention.pk),
        "entity_ref": mention.entity_ref,
        "original_label": mention.label,
        "kind": mention.kind,
        "state": mention.state,
        "candidate_id": str(candidate.pk),
        "source_run": candidate.source_run,
        "group_id": candidate.group_id,
        "canonical_id": str(mention.canonical_entity_id)
        if mention.canonical_entity_id
        else None,
        "article": article_data(candidate.version),
        "context": candidate.fact,
        "qualifiers": mention.qualifiers,
        "anchors": [
            {
                "quote": a.quote,
                "passage_id": str(a.passage_id),
                "start": a.start,
                "end": a.end,
                "locator": a.passage.locator,
            }
            for a in anchors
        ],
    }


def decision_data(decision):
    return {
        "id": str(decision.pk),
        "action": decision.action,
        "origin": decision.origin,
        "reason": decision.reason,
        "before": decision.before,
        "after": decision.after,
        "actor": decision.actor.get_username() if decision.actor_id else None,
        "created_at": decision.created_at.isoformat(),
    }


def paginate(request, query, serialize):
    try:
        limit, offset = (
            int(request.GET.get("limit", 30)),
            int(request.GET.get("offset", 0)),
        )
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
    except ValueError:
        return response({"error": "limit须为1–100，offset须为非负整数。"}, 400)
    return response(
        {
            "count": query.count(),
            "limit": limit,
            "offset": offset,
            "results": [serialize(row) for row in query[offset : offset + limit]],
        }
    )


@require_GET
def summary(request):
    entities = list(
        CanonicalEntity.objects.values("kind", "level", "source_status", "status")
    )
    entity_counts = {"total": len(entities)}
    for field, values in [
        ("kind", ["drug", "disease"]),
        ("level", ["main", "subtype", "unassigned"]),
        ("source_status", ["seed", "literature_new"]),
        ("status", ["candidate"]),
    ]:
        entity_counts.update(
            {value: sum(r[field] == value for r in entities) for value in values}
        )
    rules = Counter(EntityResolutionRule.objects.values_list("status", flat=True))
    mentions = Counter(EntityRegistryMention.objects.values_list("state", flat=True))
    bootstrap = (
        EntityRegistryImport.objects.filter(source_kind="bootstrap")
        .order_by("created_at")
        .first()
    )
    return response(
        {
            "entities": entity_counts,
            "rules": {
                "total": sum(rules.values()),
                **{k: rules[k] for k in service.RULE_STATUSES},
            },
            "mentions": {
                "total": sum(mentions.values()),
                **{
                    k: mentions[k]
                    for k in ["resolved", "proposed", "unresolved", "ignored"]
                },
            },
            "bootstrap": {
                "sealed": bootstrap is not None,
                "batch_id": bootstrap.batch_id if bootstrap else None,
                "source": bootstrap.source if bootstrap else {},
            },
            "can_review": can_review(request),
            "target_runtime_lookup": False,
        }
    )


@require_GET
def entities(request, pk=None):
    query = entity_queryset()
    if pk is not None:
        entity = get_object_or_404(query, pk=pk)
        data = entity_data(entity)
        data["rules"] = [
            rule_data(r)
            for r in entity.rules.select_related("canonical_entity__main_cancer")[:100]
        ]
        data["mentions"] = [
            mention_data(m)
            for m in entity.mentions.select_related("candidate__version__material")[
                :100
            ]
        ]
        data["children"] = [
            entity_data(e) for e in entity_queryset().filter(main_cancer=entity)[:100]
        ]
        data["decisions"] = [
            decision_data(d)
            for d in EntityRegistryDecision.objects.filter(
                subject_kind="entity", subject_id=entity.pk
            )
            .select_related("actor")
            .order_by("-created_at")[:30]
        ]
        return response(data)
    for field in ["kind", "level", "status", "source_status"]:
        if value := request.GET.get(field):
            query = query.filter(**{field: value})
    if main := request.GET.get("main_id"):
        try:
            query = query.filter(main_cancer_id=main)
        except ValidationError:
            return response({"error": "无效主癌种ID。"}, 400)
    if value := request.GET.get("is_blood_tumor"):
        if value == "unknown":
            query = query.filter(Q(provenance__is_blood_tumor__isnull=True))
        elif value in {"true", "false"}:
            query = query.filter(provenance__is_blood_tumor=value == "true")
        else:
            return response({"error": "is_blood_tumor须为true/false/unknown。"}, 400)
    if needle := request.GET.get("q", "").strip():
        alias = EntityResolutionRule.objects.filter(
            canonical_entity_id=OuterRef("pk"), alias__icontains=needle
        )
        query = query.annotate(alias_match=Exists(alias)).filter(
            Q(name__icontains=needle)
            | Q(name_en__icontains=needle)
            | Q(alias_match=True)
        )
    return paginate(request, query, entity_data)


@require_GET
def rules(request, pk=None):
    query = EntityResolutionRule.objects.select_related("canonical_entity__main_cancer")
    if pk is not None:
        rule = get_object_or_404(query, pk=pk)
        data = rule_data(rule)
        data["mentions"] = [
            mention_data(m)
            for m in matching_mentions(rule).select_related(
                "candidate__version__material"
            )[:100]
        ]
        data["decisions"] = [
            decision_data(d)
            for d in EntityRegistryDecision.objects.filter(
                subject_kind="rule", subject_id=rule.pk
            )
            .select_related("actor")
            .order_by("-created_at")[:30]
        ]
        return response(data)
    for field in ["kind", "status"]:
        if value := request.GET.get(field):
            query = (
                query.filter(status__in=["proposed", "unresolved"])
                if field == "status" and value == "pending"
                else query.filter(**{field: value})
            )
    if value := request.GET.get("has_mentions"):
        if value not in {"true", "false"}:
            return response({"error": "has_mentions须为true/false。"}, 400)
        # Global aliases match every article; scoped aliases match their own version.
        global_matches = EntityRegistryMention.objects.filter(
            kind=OuterRef("kind"), normalized_label=OuterRef("normalized_alias")
        )
        scoped_matches = global_matches.filter(scope_key=OuterRef("scope_key"))
        query = query.annotate(
            global_used=Exists(global_matches), scoped_used=Exists(scoped_matches)
        )
        condition = Q(scope_type="global", global_used=True) | Q(
            scope_type="article", scoped_used=True
        )
        query = query.filter(condition) if value == "true" else query.exclude(condition)
    if value := request.GET.get("canonical_id"):
        try:
            query = query.filter(canonical_entity_id=value)
        except ValidationError:
            return response({"error": "无效实体ID。"}, 400)
    if needle := request.GET.get("q", "").strip():
        query = query.filter(
            Q(alias__icontains=needle) | Q(canonical_entity__name__icontains=needle)
        )
    return paginate(request, query, rule_data)


def write_request(request, operation, pk):
    if not request.user.is_authenticated:
        return response({"error": "请先登录。"}, 401)
    if not can_review(request):
        return response({"error": "当前账号没有实体审核权限。"}, 403)
    try:
        if len(request.body) > 65536:
            raise ValueError("审核请求过大。")
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise TypeError("审核请求须为对象。")
        obj, changed = operation(pk, data, request.user)
        key, serialize = (
            ("rule", rule_data)
            if isinstance(obj, EntityResolutionRule)
            else ("entity", entity_data)
        )
        return response(
            {
                key: serialize(obj),
                "affected_mentions": changed["affected"],
                "changed_mentions": changed["changed"],
            }
        )
    except service.RegistryConflict as exc:
        return response({"error": str(exc)}, 409)
    except OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        return response({"error": "另一个操作正在更新数据，请稍后刷新重试。"}, 409)
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        return response({"error": str(exc)}, 400)
    except ObjectDoesNotExist:
        return response({"error": "实体或规则不存在。"}, 404)


@require_POST
def review_rule(request, pk):
    return write_request(request, service.review_rule, pk)


@require_POST
def review_entity(request, pk):
    return write_request(request, service.review_entity, pk)
