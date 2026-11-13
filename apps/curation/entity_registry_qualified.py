"""Derive source-anchored disease aliases from confirmed complete local names."""

import re

from django.db import transaction

from apps.curation.entity_registry import (
    SCHEMA,
    digest,
    import_registry,
    normalize,
    rule_snapshot,
)
from apps.curation.entity_registry_models import (
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import KnowledgeAnchor
from apps.curation.qualified_disease_names import negative_before, source_definitions

POLICY = "advanced-orthographic-confirmed-identity-v1"
DASHES = str.maketrans("‐‑‒–—", "-----")


def _normalized_forms(text):
    return {normalize(text), normalize(text.translate(DASHES))}


def _components(label):
    """Preserve the exact original qualifier; never strip molecular criteria."""
    original = label.strip()
    qualified = re.fullmatch(r"(locally\s+advanced\s+or\s+metastatic|advanced)\s+(.+)", original, re.IGNORECASE)
    qualifier, rest = (qualified.group(1), qualified.group(2)) if qualified else (None, original)
    parenthetical = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", rest)
    parts = [part.strip() for part in parenthetical.groups()] if parenthetical else [rest.strip()]
    if (not all(parts) or (len(parts) == 2 and normalize(parts[0]) == normalize(parts[1]))
            or (not qualifier and normalize(original) == normalize(original.translate(DASHES)))):
        return None
    return qualifier, parts


def _source_anchor(mention):
    anchors = KnowledgeAnchor.objects.filter(
        candidate_id=mention.candidate_id,
        entity_ref=mention.entity_ref,
        entity_id=mention.entity_id,
        purpose="mention",
        quote=mention.label,
    ).select_related("passage")
    for anchor in anchors:
        if (anchor.association not in {"model_selected", "exact_fact_text"}
                or not isinstance(anchor.roles, list) or "disease" not in anchor.roles
                or anchor.passage.version_id != mention.candidate.version_id
                or anchor.start < 0 or anchor.end - anchor.start != len(mention.label)
                or anchor.passage.text[anchor.start:anchor.end] != mention.label
                or negative_before(anchor.passage.text, anchor.start)):
            continue
        return {"candidate_id": str(mention.candidate_id), "source_run": mention.candidate.source_run,
                "group_id": mention.candidate.group_id, "entity_ref": mention.entity_ref,
                "version_id": str(mention.candidate.version_id), "source_sha256": mention.candidate.version.sha256,
                "passage_id": str(anchor.passage_id), "start": anchor.start, "end": anchor.end,
                "quote": anchor.quote}
    return None


def _rules(names, *, lock):
    query = EntityResolutionRule.objects.filter(kind="disease", normalized_alias__in=names).select_related("canonical_entity")
    if lock:
        query = query.select_for_update()
    return list(query.order_by("scope_type", "scope_key", "normalized_alias", "id"))


def _applicable(rules, scope_key):
    return [rule for rule in rules if rule.scope_type == "global" or (rule.scope_type == "article" and rule.scope_key == scope_key)]


def _confirmed(rules):
    return bool(rules) and all(
        rule.status == "confirmed" and rule.action != "ignore"
        and rule.canonical_entity_id and rule.canonical_entity.status == "active"
        and rule.canonical_entity.kind == "disease" for rule in rules
    ) and len({rule.canonical_entity_id for rule in rules}) == 1


def _qualifiers(rules, label, qualifier):
    result = [qualifier] if qualifier else []
    for rule in rules:
        if rule.provenance.get("match_relation") != "qualified_form":
            continue
        existing = rule.provenance.get("required_qualifiers")
        if not isinstance(existing, list) or not existing:
            return None
        for value in existing:
            if not isinstance(value, str) or not value.strip() or normalize(value) not in normalize(label):
                return None
            if normalize(value) not in {normalize(item) for item in result}:
                result.append(value)
    return result


def build_qualified_alias_batch(candidate_ids, *, lock=False):
    entries, skipped, seen = [], [], set()
    mentions = EntityRegistryMention.objects.filter(
        candidate_id__in=candidate_ids, candidate__version__integrity_status="valid",
        kind="disease", state__in=["unresolved", "proposed"],
    ).select_related("candidate__version").order_by("candidate__source_run", "candidate__group_id", "entity_ref")
    for mention in mentions:
        components = _components(mention.label)
        identity = (normalize(mention.label), mention.scope_key)
        if components is None or identity in seen:
            continue
        support = _source_anchor(mention)
        if not support:
            skipped.append({"label": mention.label, "reason": "precise_disease_source_anchor_required"})
            continue
        qualifier, parts = components
        full_rules = _rules(_normalized_forms(mention.label), lock=lock)
        applicable_full = _applicable(full_rules, mention.scope_key)
        if any(rule.normalized_alias == normalize(mention.label) for rule in applicable_full):
            skipped.append({"label": mention.label, "reason": "existing_full_name_decision_preserved"})
            continue
        component_rules, safe_global = [], True
        for part in parts:
            all_rules = _rules(_normalized_forms(part), lock=lock)
            applicable = _applicable(all_rules, mention.scope_key)
            if not _confirmed(applicable):
                component_rules = []
                break
            component_rules.extend(rule for rule in applicable if rule not in component_rules)
            safe_global &= any(rule.scope_type == "global" for rule in applicable)
            safe_global &= all(rule.scope_type == "global" or rule.scope_key == mention.scope_key for rule in all_rules)
        if not _confirmed(component_rules):
            skipped.append({"label": mention.label, "reason": "base_names_not_uniquely_confirmed"})
            continue
        canonical = component_rules[0].canonical_entity
        if applicable_full and (not _confirmed(applicable_full)
                                or applicable_full[0].canonical_entity_id != canonical.pk):
            skipped.append({"label": mention.label, "reason": "equivalent_name_conflict_preserved"})
            continue
        safe_global &= all(rule.scope_type == "global" or rule.scope_key == mention.scope_key for rule in full_rules)
        basis_rules = component_rules + [rule for rule in applicable_full if rule not in component_rules]
        qualifiers = _qualifiers(basis_rules, mention.label, qualifier)
        if qualifiers is None:
            skipped.append({"label": mention.label, "reason": "required_qualifier_not_in_source_name"})
            continue
        scope = {"type": "global", "key": ""} if safe_global else {"type": "article", "key": mention.scope_key}
        # One global rule can cover multiple articles within this chunk.
        batch_identity = (normalize(mention.label), scope["type"], scope["key"])
        if batch_identity in seen:
            continue
        seen.update({identity, batch_identity})
        entries.append({
            "kind": "disease", "alias": mention.label, "scope": scope,
            "canonical_key": canonical.key, "action": "alias", "status": "confirmed", "origin": "rule",
            "reason": "原文完整疾病名经明确的advanced限定和连字符字形整理后，各组成名均由本地已确认规则指向同一有效实体；原词与必要限定保留。",
            "evidence": [support], "alternatives": [],
            "provenance": {
                "derivation_policy": POLICY, "match_relation": "qualified_form" if qualifiers else "alias",
                "required_qualifiers": qualifiers, "source_name": mention.label, "component_names": parts,
                "source_rules": [{"id": str(rule.pk), "revision": rule.revision,
                                  "state_sha256": digest(rule_snapshot(rule)), "alias": rule.alias,
                                  "scope": {"type": rule.scope_type, "key": rule.scope_key}}
                                 for rule in basis_rules],
                "human_confirmed": False, "model_called": False, "target_contacted": False,
            },
        })
    return ({"schema_version": SCHEMA, "batch_id": "entity-registry-qualified-v1-" + digest(entries)[:20],
             "source": {"kind": "literature", "policy": POLICY, "target_contacted": False, "model_called": False},
             "entities": [], "rules": entries, "mentions": [], "classifications": []}, skipped)


@transaction.atomic
def derive_qualified_aliases(candidate_ids):
    payload, skipped = build_qualified_alias_batch(candidate_ids, lock=True)
    if not payload["rules"]:
        return {"created_rules": 0, "skipped": skipped, "model_called": False, "target_contacted": False}
    result = import_registry(payload)
    return {**result, "skipped": skipped, "model_called": False}


def build_article_abbreviation_batch(candidate_ids, *, lock=False):
    """Use an explicit same-article definition, never a global abbreviation."""
    entries, skipped, seen = [], [], set()
    mentions = EntityRegistryMention.objects.filter(
        candidate_id__in=candidate_ids, candidate__version__integrity_status="valid",
        kind="disease", state__in=["unresolved", "proposed"],
    ).select_related("candidate__version").order_by("candidate__source_run", "candidate__group_id", "entity_ref")
    for mention in mentions:
        identity = (normalize(mention.label), mention.scope_key)
        if identity in seen or not _source_anchor(mention):
            continue
        existing = _applicable(_rules(_normalized_forms(mention.label), lock=lock), mention.scope_key)
        if existing:
            skipped.append({"label": mention.label, "reason": "existing_abbreviation_decision_preserved"})
            continue
        anchors, valid_anchors = [], []
        for anchor in KnowledgeAnchor.objects.filter(candidate_id=mention.candidate_id, purpose__in=["evidence", "context"]).select_related("passage"):
            if (anchor.passage.version_id != mention.candidate.version_id or anchor.start < 0
                    or anchor.end - anchor.start != len(anchor.quote)
                    or anchor.passage.text[anchor.start:anchor.end] != anchor.quote):
                continue
            anchors.append({"purpose": anchor.purpose, "passage_id": str(anchor.passage_id), "quote": anchor.quote})
            valid_anchors.append(anchor)
        definitions = source_definitions(anchors, mention.label)
        if not definitions:
            continue
        canonical_ids, all_rules, qualifiers = set(), [], None
        for definition in definitions:
            base_rules = _applicable(_rules(_normalized_forms(definition["base"]), lock=lock), mention.scope_key)
            full_rules = _applicable(_rules(_normalized_forms(definition["full_name"]), lock=lock), mention.scope_key)
            if not _confirmed(base_rules) or (full_rules and not _confirmed(full_rules)):
                canonical_ids = set()
                break
            basis = base_rules + [rule for rule in full_rules if rule not in base_rules]
            if not _confirmed(basis):
                canonical_ids = set()
                break
            required = _qualifiers(basis, definition["full_name"], definition["qualifier"])
            if required is None or (qualifiers is not None and qualifiers != required):
                canonical_ids = set()
                break
            qualifiers = required
            canonical_ids.add(basis[0].canonical_entity_id)
            all_rules.extend(rule for rule in basis if rule not in all_rules)
        if len(canonical_ids) != 1 or not qualifiers:
            skipped.append({"label": mention.label, "reason": "source_definition_not_uniquely_confirmed"})
            continue
        evidence = []
        for definition in definitions:
            source = valid_anchors[definition["anchor_index"]]
            evidence.append({"candidate_id": str(mention.candidate_id), "source_run": mention.candidate.source_run,
                             "group_id": mention.candidate.group_id, "entity_ref": mention.entity_ref,
                             "version_id": str(mention.candidate.version_id), "source_sha256": mention.candidate.version.sha256,
                             "passage_id": definition["passage_id"], "start": source.start + definition["start_in_anchor"],
                             "end": source.start + definition["end_in_anchor"], "quote": definition["quote"]})
        seen.add(identity)
        entries.append({
            "kind": "disease", "alias": mention.label, "scope": {"type": "article", "key": mention.scope_key},
            "canonical_key": all_rules[0].canonical_entity.key, "action": "alias", "status": "confirmed", "origin": "rule",
            "reason": "本文原文将缩写明确置于已确认癌种全名及局部晚期或转移性限定之后；只在该文章内复用并保留限定。",
            "evidence": evidence, "alternatives": [],
            "provenance": {
                "derivation_policy": "source-defined-qualified-abbreviation-v1", "match_relation": "qualified_form",
                "required_qualifiers": qualifiers, "source_name": mention.label,
                "source_definitions": [item["full_name"] for item in definitions],
                "source_rules": [{"id": str(rule.pk), "revision": rule.revision,
                                  "state_sha256": digest(rule_snapshot(rule)), "alias": rule.alias,
                                  "scope": {"type": rule.scope_type, "key": rule.scope_key}} for rule in all_rules],
                "human_confirmed": False, "model_called": False, "target_contacted": False,
            },
        })
    return ({"schema_version": SCHEMA, "batch_id": "entity-registry-scoped-abbr-v1-" + digest(entries)[:20],
             "source": {"kind": "literature", "policy": "source-defined-qualified-abbreviation-v1",
                        "target_contacted": False, "model_called": False},
             "entities": [], "rules": entries, "mentions": [], "classifications": []}, skipped)


@transaction.atomic
def derive_article_abbreviations(candidate_ids):
    payload, skipped = build_article_abbreviation_batch(candidate_ids, lock=True)
    if not payload["rules"]:
        return {"created_rules": 0, "skipped": skipped, "model_called": False, "target_contacted": False}
    return {**import_registry(payload), "skipped": skipped, "model_called": False}
