"""Derive one article alias only when both parenthetical forms already agree."""

import re
import unicodedata

from django.db import transaction
from django.db.models import Q

from apps.curation.entity_registry import (
    SCHEMA,
    digest,
    import_registry,
    normalize,
    rule_snapshot,
    validate_alias_target,
)
from apps.curation.entity_registry_models import (
    EntityRegistryMention,
    EntityResolutionRule,
)

POLICY = "parenthetical-confirmed-same-identity-v1"


def split_parenthetical_name(label):
    match = re.fullmatch(
        r"([^()]+?)\s*\(([^()]+)\)", unicodedata.normalize("NFKC", label).strip()
    )
    if not match:
        return None
    left, right = (part.strip() for part in match.groups())
    if not left or not right or normalize(left) == normalize(right):
        return None
    return left, right


def build_parenthetical_alias_batch(candidate_ids, *, lock=False):
    rules, skipped, seen = [], [], set()
    mentions = EntityRegistryMention.objects.filter(
        candidate_id__in=candidate_ids, state__in=["unresolved", "proposed"]
    ).select_related("candidate")
    for mention in mentions:
        parts = split_parenthetical_name(mention.label)
        identity = (mention.kind, normalize(mention.label), mention.scope_key)
        if parts is None or identity in seen:
            continue
        seen.add(identity)
        if (
            EntityResolutionRule.objects.filter(
                kind=mention.kind, normalized_alias=normalize(mention.label)
            )
            .filter(
                Q(scope_type="global")
                | Q(scope_type="article", scope_key=mention.scope_key)
            )
            .exists()
        ):
            skipped.append(
                {"label": mention.label, "reason": "existing_rule_preserved"}
            )
            continue
        component_rules = (
            EntityResolutionRule.objects.filter(
                kind=mention.kind, normalized_alias__in=[normalize(p) for p in parts]
            )
            .filter(
                Q(scope_type="global")
                | Q(scope_type="article", scope_key=mention.scope_key)
            )
            .select_related("canonical_entity")
        )
        if lock:
            component_rules = component_rules.select_for_update()
        component_rules = list(
            component_rules.order_by("scope_type", "normalized_alias", "id")
        )
        if {rule.normalized_alias for rule in component_rules} != {
            normalize(p) for p in parts
        }:
            skipped.append(
                {"label": mention.label, "reason": "both_forms_must_be_known"}
            )
            continue
        if any(
            rule.status != "confirmed"
            or rule.action == "ignore"
            or rule.canonical_entity_id is None
            or rule.canonical_entity.status != "active"
            or rule.canonical_entity.kind != mention.kind
            for rule in component_rules
        ):
            skipped.append(
                {"label": mention.label, "reason": "both_forms_must_be_confirmed"}
            )
            continue
        if len({rule.canonical_entity_id for rule in component_rules}) != 1:
            skipped.append(
                {"label": mention.label, "reason": "conflicting_identity_or_scope"}
            )
            continue
        try:
            validate_alias_target(mention.label, component_rules[0].canonical_entity)
        except ValueError:
            skipped.append(
                {
                    "label": mention.label,
                    "reason": "combination_target_requires_explicit_identity",
                }
            )
            continue
        qualifiers = []
        for rule in component_rules:
            if rule.provenance.get("match_relation") == "qualified_form":
                required = rule.provenance.get("required_qualifiers")
                if (
                    not isinstance(required, list)
                    or not required
                    or not all(isinstance(value, str) and value for value in required)
                ):
                    qualifiers = None
                    break
                qualifiers.extend(
                    value for value in required if value not in qualifiers
                )
        if qualifiers is None:
            skipped.append(
                {"label": mention.label, "reason": "unsupported_qualifier_shape"}
            )
            continue
        rules.append(
            {
                "kind": mention.kind,
                "alias": mention.label,
                "scope": {"type": "article", "key": mention.scope_key},
                "canonical_key": component_rules[0].canonical_entity.key,
                "action": "alias",
                "status": "confirmed",
                "origin": "rule",
                "reason": "完整名称与括号内名称均由本地已确认规则独立指向同一实体，且当前文章与全局范围无冲突；仅登记本文章组合原名。",
                "alternatives": [],
                "evidence": [
                    {
                        "candidate_id": str(mention.candidate_id),
                        "source_run": mention.candidate.source_run,
                        "group_id": mention.candidate.group_id,
                        "entity_ref": mention.entity_ref,
                        "source_label": mention.label,
                    }
                ],
                "provenance": {
                    "derivation_policy": POLICY,
                    "parenthetical_forms": list(parts),
                    "match_relation": "qualified_form" if qualifiers else "alias",
                    "required_qualifiers": qualifiers,
                    "source_rules": [
                        {
                            "id": str(rule.pk),
                            "revision": rule.revision,
                            "state_sha256": digest(rule_snapshot(rule)),
                            "alias": rule.alias,
                            "scope": {"type": rule.scope_type, "key": rule.scope_key},
                        }
                        for rule in component_rules
                    ],
                    "human_confirmed": False,
                    "model_called": False,
                    "target_contacted": False,
                },
            }
        )
    payload = {
        "schema_version": SCHEMA,
        "batch_id": "entity-registry-parenthetical-v1-" + digest(rules)[:20],
        "source": {
            "kind": "literature",
            "policy": POLICY,
            "target_contacted": False,
            "model_called": False,
        },
        "entities": [],
        "rules": rules,
        "mentions": [],
        "classifications": [],
    }
    return payload, skipped


@transaction.atomic
def derive_parenthetical_aliases(candidate_ids, *, dry_run=False):
    payload, skipped = build_parenthetical_alias_batch(candidate_ids, lock=True)
    if not payload["rules"]:
        return {
            "created_rules": 0,
            "skipped": skipped,
            "model_called": False,
            "target_contacted": False,
        }
    result = import_registry(payload, dry_run=dry_run)
    return {**result, "skipped": skipped, "model_called": False}
