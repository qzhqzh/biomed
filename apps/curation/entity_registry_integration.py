"""Reuse local identity decisions at knowledge intake and in role-bound search."""

from collections import Counter

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q

from apps.curation.entity_registry import RegistryConflict, normalize, resolve_mentions
from apps.curation.entity_registry_models import (
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import (
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeSearchTerm,
)

PARTICIPANT_ROLES = {"drug": "intervention", "disease": "disease"}


def legacy_participant_roles(candidate, anchors, approved):
    """Only explicit old relationship anchors or the current approved review.

    Missing legacy fact roles are not permission to promote every mention. A
    review without an entity_ref is usable only when that entity has one ref.
    """
    roles, bases, entity_refs = {}, {}, {}
    for ref, linked in anchors.items():
        for anchor in linked:
            entity_refs.setdefault(anchor.entity_id, set()).add(ref)
            role = PARTICIPANT_ROLES[anchor.entity.kind]
            if (
                anchor.purpose == "mention"
                and anchor.association in {"model_selected", "exact_fact_text"}
                and isinstance(anchor.roles, list)
                and role in anchor.roles
            ):
                roles[ref] = [role]
                bases[ref] = {"participation_basis": "relationship_anchor"}
    if candidate.current_review_id:
        links = candidate.current_review.result.get("approved_links", [])
        for entity_id, kind in approved:
            explicit = {
                link.get("entity_ref")
                for link in links
                if link.get("entity_id") == str(entity_id)
                and link.get("role") == kind
                and link.get("entity_ref")
            }
            refs = explicit or (
                entity_refs.get(entity_id, set())
                if len(entity_refs.get(entity_id, set())) == 1
                else set()
            )
            for ref in refs:
                linked = anchors.get(ref, [])
                if (
                    linked
                    and {anchor.entity_id for anchor in linked} == {entity_id}
                    and linked[0].entity.kind == kind
                ):
                    roles[ref] = [PARTICIPANT_ROLES[kind]]
                    bases[ref] = {
                        "participation_basis": "current_approved_review",
                        "participation_review_id": str(candidate.current_review_id),
                    }
    return roles, bases


def normalized_participant_data(mention):
    canonical = mention.canonical_entity
    rule = mention.applied_rule
    parent = canonical.main_cancer if canonical else None
    return {
        "entity_ref": mention.entity_ref,
        "original_label": mention.label,
        "state": mention.state,
        "canonical": {
            "id": str(canonical.pk),
            "key": canonical.key,
            "name": canonical.name,
            "kind": canonical.kind,
            "level": canonical.level,
            "entity_level": canonical.provenance.get("entity_level")
            or canonical.provenance.get("local_standard", {}).get("entity_level"),
            "main_cancer": {
                "id": str(parent.pk),
                "key": parent.key,
                "name": parent.name,
            }
            if parent
            else None,
        }
        if canonical
        else None,
        "applied_rule": {
            "id": str(rule.pk),
            "alias": rule.alias,
            "status": rule.status,
            "origin": rule.origin,
            "scope": {"type": rule.scope_type, "key": rule.scope_key},
            "match_relation": rule.provenance.get("match_relation"),
        }
        if rule
        else None,
        "qualifiers": mention.qualifiers,
    }


def register_candidates(candidate_ids, *, chunk_size=50):
    """Append derived mentions in bounded transactions; never alter source facts.

    This can resume after a candidate import committed but normalization failed.
    Existing experimental qualifiers and human decisions are never overwritten.
    """
    from apps.curation.entity_registry_parenthetical import derive_parenthetical_aliases
    from apps.curation.entity_registry_qualified import (
        derive_article_abbreviations,
        derive_qualified_aliases,
    )

    if not 1 <= chunk_size <= 200:
        raise ValueError("Registry chunk_size must be 1–200")
    ids = list(dict.fromkeys(candidate_ids))
    result = {
        "candidates": 0,
        "created_mentions": 0,
        "existing_mentions": 0,
        "skipped_invalid_sources": 0,
        "affected": 0,
        "changed": 0,
        "derived_parenthetical_rules": 0,
        "derived_qualified_rules": 0,
        "derived_article_abbreviation_rules": 0,
        "target_contacted": False,
        "model_called": False,
    }
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset : offset + chunk_size]
        candidates = list(
            KnowledgeCandidate.objects.filter(pk__in=chunk).select_related(
                "version", "current_review"
            )
        )
        legacy_ids = [c.pk for c in candidates if "entity_roles" not in c.fact]
        approved = {}
        for term in KnowledgeSearchTerm.objects.filter(
            candidate_id__in=legacy_ids,
            review_id=F("candidate__current_review_id"),
            review__candidate_id=F("candidate_id"),
            review__kind="ai",
            review__decision="pass",
            role__in=PARTICIPANT_ROLES,
        ):
            approved.setdefault(term.candidate_id, set()).add(
                (term.entity_id, term.role)
            )
        anchors = {}
        for anchor in KnowledgeAnchor.objects.filter(
            candidate_id__in=chunk, entity__kind__in=PARTICIPANT_ROLES
        ).select_related("entity"):
            anchors.setdefault((anchor.candidate_id, anchor.entity_ref), []).append(
                anchor
            )
        prepared = []
        for candidate in candidates:
            result["candidates"] += 1
            if candidate.version.integrity_status != "valid":
                result["skipped_invalid_sources"] += 1
                continue
            bases = {}
            if "entity_roles" in candidate.fact:
                roles = candidate.fact["entity_roles"]
            else:
                roles, bases = legacy_participant_roles(
                    candidate,
                    {
                        ref: linked
                        for (pk, ref), linked in anchors.items()
                        if pk == candidate.pk
                    },
                    approved.get(candidate.pk, set()),
                )
            if not isinstance(roles, dict):
                continue
            for ref, assigned_roles in roles.items():
                if not isinstance(assigned_roles, list):
                    continue
                linked = anchors.get((candidate.pk, ref), [])
                if not linked:
                    continue
                entities = {anchor.entity_id for anchor in linked}
                if len(entities) != 1:
                    raise RegistryConflict(
                        "One participating reference has conflicting source entities"
                    )
                original = linked[0].entity
                if PARTICIPANT_ROLES[original.kind] not in assigned_roles:
                    continue
                prepared.append(
                    (
                        candidate,
                        ref,
                        original,
                        bases.get(ref, {"participation_basis": "fact_roles"}),
                    )
                )
        with transaction.atomic():
            valid_ids = set(
                KnowledgeCandidate.objects.filter(
                    pk__in=chunk, version__integrity_status="valid"
                ).values_list("pk", flat=True)
            )
            current_reviews = dict(
                KnowledgeCandidate.objects.filter(pk__in=chunk).values_list(
                    "pk", "current_review_id"
                )
            )
            for candidate, ref, original, basis in prepared:
                if candidate.pk not in valid_ids:
                    continue
                if (
                    basis["participation_basis"] == "current_approved_review"
                    and current_reviews[candidate.pk] != candidate.current_review_id
                ):
                    continue
                defaults = {
                    "entity": original,
                    "kind": original.kind,
                    "label": original.label,
                    "normalized_label": normalize(original.label),
                    "scope_key": str(candidate.version_id),
                }
                mention, created = EntityRegistryMention.objects.get_or_create(
                    candidate=candidate,
                    entity_ref=ref,
                    defaults={
                        **defaults,
                        "qualifiers": {
                            "registration": "knowledge_import",
                            "original_label": original.label,
                            "source_sha256": candidate.version.sha256,
                            **basis,
                        },
                    },
                )
                if not created and any(
                    getattr(mention, key) != value for key, value in defaults.items()
                ):
                    raise RegistryConflict(
                        "Registered source mention disagrees with the immutable candidate"
                    )
                result["created_mentions" if created else "existing_mentions"] += 1
            resolution = resolve_mentions(candidate_ids=chunk)
            result["affected"] += resolution["affected"]
            result["changed"] += resolution["changed"]
        # Derivation uses a second short transaction, after source registration.
        derived = derive_parenthetical_aliases(chunk)
        result["derived_parenthetical_rules"] += derived["created_rules"]
        derived = derive_qualified_aliases(chunk)
        result["derived_qualified_rules"] += derived["created_rules"]
        derived = derive_article_abbreviations(chunk)
        result["derived_article_abbreviation_rules"] += derived["created_rules"]
    result["states"] = dict(
        Counter(
            EntityRegistryMention.objects.filter(candidate_id__in=ids).values_list(
                "state", flat=True
            )
        )
    )
    return result


def registry_match_query(term, *, kind=None, published=False, contains=False):
    """Correlated query: all identities must participate in this same candidate.

    A qualified alias is never expanded to the unqualified parent population. An
    article-only abbreviation is usable only in its original article scope.
    Published search additionally requires the same entity in the current review.
    """
    lookup = "icontains" if contains else "iexact"
    normalized_lookup = "icontains" if contains else "exact"
    query = EntityRegistryMention.objects.filter(
        candidate_id=OuterRef("pk"),
        state="resolved",
        applied_rule__status="confirmed",
        canonical_entity__status="active",
    )
    if kind:
        query = query.filter(kind=kind)
    aliases = (
        EntityResolutionRule.objects.filter(
            status="confirmed", canonical_entity__status="active"
        )
        .exclude(action="ignore")
        .filter(
            Q(canonical_entity_id=OuterRef("canonical_entity_id"))
            | Q(canonical_entity_id=OuterRef("canonical_entity__main_cancer_id")),
            Q(scope_type="global")
            | Q(scope_type="article", scope_key=OuterRef("scope_key")),
            Q(provenance__match_relation__isnull=True)
            | ~Q(provenance__match_relation="qualified_form"),
            **{f"normalized_alias__{normalized_lookup}": normalize(term)},
        )
    )
    condition = (
        Q(**{f"canonical_entity__name__{lookup}": term})
        | Q(**{f"canonical_entity__name_en__{lookup}": term})
        | Q(
            canonical_entity__main_cancer__status="active",
            **{f"canonical_entity__main_cancer__name__{lookup}": term},
        )
        | Q(
            canonical_entity__main_cancer__status="active",
            **{f"canonical_entity__main_cancer__name_en__{lookup}": term},
        )
        | Q(**{f"normalized_label__{normalized_lookup}": normalize(term)})
        | Q(registry_alias_match=True)
    )
    query = query.annotate(registry_alias_match=Exists(aliases)).filter(condition)
    if published:
        approved = KnowledgeSearchTerm.objects.filter(
            candidate_id=OuterRef("candidate_id"),
            review_id=OuterRef("candidate__current_review_id"),
            entity_id=OuterRef("entity_id"),
        )
        if kind:
            approved = approved.filter(role=kind)
        else:
            approved = approved.filter(role__in=PARTICIPANT_ROLES)
        query = query.filter(Exists(approved))
    return query
