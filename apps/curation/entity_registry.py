"""Local entity registry operations. No remote lookup and no knowledge rewrites."""

import hashlib
import json
import re
import unicodedata
import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.curation.entity_registry_models import (
    CanonicalEntity,
    EntityRegistryDecision,
    EntityRegistryImport,
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import KnowledgeAnchor, KnowledgeCandidate

SCHEMA = "biomed.entity-registry.v1"
NAMESPACE = uuid.UUID("4bbf5af2-a18a-54c9-9b06-7f781bb46c46")
KINDS = {"drug", "disease"}
LEVELS = {"main", "subtype", "unassigned", "not_applicable"}
RULE_STATUSES = {"confirmed", "proposed", "unresolved", "rejected", "revoked"}
ORIGINS = {"dictionary", "rule", "model", "user_instruction"}


class RegistryConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def normalize(value):
    return (
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)))
        .strip()
        .casefold()
    )


def text(value, name, maximum=2000, *, required=True):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (required and not value.strip())
    ):
        raise ValueError(f"{name} 必须是有效文本。")
    return value.strip()


def scope_data(value):
    if not isinstance(value, dict) or value.get("type") not in {"global", "article"}:
        raise ValueError("scope 必须为 global 或 article。")
    key = value.get("key", "")
    if value["type"] == "global":
        if key:
            raise ValueError("全局别名不能附带文章范围。")
    else:
        try:
            key = str(uuid.UUID(str(key)))
        except ValueError as exc:
            raise ValueError("文章范围须为原文版本 UUID。") from exc
    return value["type"], key


def rule_identity(kind, alias, scope_type="global", scope_key=""):
    return digest([kind, normalize(alias), scope_type, scope_key])


def entity_snapshot(entity):
    return {
        "id": str(entity.pk),
        "key": entity.key,
        "kind": entity.kind,
        "name": entity.name,
        "name_en": entity.name_en,
        "level": entity.level,
        "main_id": str(entity.main_cancer_id) if entity.main_cancer_id else None,
        "status": entity.status,
        "source_status": entity.source_status,
        "external_ids": entity.external_ids,
        "provenance": entity.provenance,
        "revision": entity.revision,
    }


def rule_snapshot(rule):
    return {
        "id": str(rule.pk),
        "kind": rule.kind,
        "alias": rule.alias,
        "scope": {"type": rule.scope_type, "key": rule.scope_key},
        "canonical_id": str(rule.canonical_entity_id)
        if rule.canonical_entity_id
        else None,
        "action": rule.action,
        "status": rule.status,
        "origin": rule.origin,
        "reason": rule.reason,
        "alternatives": rule.alternatives,
        "evidence": rule.evidence,
        "provenance": rule.provenance,
        "revision": rule.revision,
    }


def validate_classification(kind, level, main=None, entity=None):
    if kind not in KINDS or level not in LEVELS:
        raise ValueError("实体类型或分类层级无效。")
    if kind == "drug" and (level != "not_applicable" or main is not None):
        raise ValueError("药物不能设置主癌种或癌种层级。")
    if kind == "disease" and level == "not_applicable":
        raise ValueError("癌种须为主癌种、亚型或待归类。")
    if level == "subtype":
        if (
            main is None
            or main.kind != "disease"
            or main.level != "main"
            or main.main_cancer_id
        ):
            raise ValueError("亚型必须归属一个主癌种。")
        if entity is not None and main.pk == entity.pk:
            raise ValueError("实体不能归属自身。")
    elif main is not None:
        raise ValueError("只有亚型可以设置主癌种。")
    if entity is not None and level != "main" and entity.subtypes.exists():
        raise ValueError("该主癌种仍有亚型，须先处理受影响分类。")


def validate_alias_target(alias, canonical):
    """A regimen's ingredients cannot silently become aliases of the whole regimen."""
    if canonical is None or canonical.kind != "drug":
        return
    if canonical.provenance.get("source_form") in {"regimen", "possible_combination"}:
        complete_names = [
            canonical.name,
            canonical.name_en,
            *canonical.provenance.get("source_aliases", []),
        ]
        if normalize(alias) not in {normalize(name) for name in complete_names if name}:
            raise ValueError("组合药条目仅可复用完整名称；单个成分须对齐独立药物实体。")


def validate_confirmable_alias(alias):
    if not any(character.isalnum() for character in alias):
        raise ValueError("纯标点来源占位符不能确认成实体名称规则。")


def _event(
    subject_kind,
    obj,
    action,
    origin,
    reason,
    before,
    after,
    *,
    actor=None,
    provenance=None,
):
    return EntityRegistryDecision.objects.create(
        event_key=digest([subject_kind, str(obj.pk), after.get("revision"), action]),
        subject_kind=subject_kind,
        subject_id=obj.pk,
        action=action,
        origin=origin,
        reason=reason,
        before=before,
        after=after,
        actor=actor,
        provenance=provenance or {},
    )


def resolve_mentions(*, kind=None, normalized_alias=None, candidate_ids=None):
    """Recompute only derived links. A scoped unresolved rule blocks global reuse."""
    mentions = EntityRegistryMention.objects.all()
    if candidate_ids is not None:
        mentions = mentions.filter(candidate_id__in=candidate_ids)
    if kind:
        mentions = mentions.filter(kind=kind)
    if normalized_alias is not None:
        mentions = mentions.filter(normalized_label=normalized_alias)
    rules = EntityResolutionRule.objects.exclude(
        status__in=["rejected", "revoked"]
    ).select_related("canonical_entity")
    if candidate_ids is not None:
        rules = rules.filter(normalized_alias__in=mentions.values("normalized_label"))
    if kind:
        rules = rules.filter(kind=kind)
    if normalized_alias is not None:
        rules = rules.filter(normalized_alias=normalized_alias)
    indexed = {
        (r.kind, r.normalized_alias, r.scope_type, r.scope_key): r for r in rules
    }
    changed, affected = [], 0
    for mention in mentions:
        affected += 1
        rule = indexed.get(
            (mention.kind, mention.normalized_label, "article", mention.scope_key)
        )
        if rule is None:
            rule = indexed.get((mention.kind, mention.normalized_label, "global", ""))
        state, canonical = "unresolved", None
        if rule is not None:
            if rule.status == "confirmed":
                if rule.action == "ignore":
                    state = "ignored"
                elif (
                    rule.canonical_entity_id
                    and rule.canonical_entity.status == "active"
                ):
                    state, canonical = "resolved", rule.canonical_entity_id
                else:
                    state = "proposed"
            elif rule.status == "proposed":
                state = "proposed"
        rule_id = rule.pk if rule else None
        qualifiers = {
            k: v for k, v in mention.qualifiers.items() if k != "rule_qualifiers"
        }
        if (
            rule is not None
            and state == "resolved"
            and rule.provenance.get("match_relation") == "qualified_form"
        ):
            qualifiers["rule_qualifiers"] = {
                "rule_id": str(rule.pk),
                "source_label": rule.alias,
                "match_relation": "qualified_form",
                "required_qualifiers": rule.provenance["required_qualifiers"],
            }
        if (
            mention.state,
            mention.canonical_entity_id,
            mention.applied_rule_id,
            mention.qualifiers,
        ) != (
            state,
            canonical,
            rule_id,
            qualifiers,
        ):
            mention.state, mention.canonical_entity_id, mention.applied_rule_id = (
                state,
                canonical,
                rule_id,
            )
            mention.updated_at = timezone.now()
            mention.qualifiers = qualifiers
            changed.append(mention)
    if changed:
        EntityRegistryMention.objects.bulk_update(
            changed,
            ["state", "canonical_entity", "applied_rule", "qualifiers", "updated_at"],
        )
    return {"affected": affected, "changed": len(changed)}


@transaction.atomic
def import_registry(payload, *, dry_run=False):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise ValueError("不支持的实体登记格式。")
    batch_id = text(payload.get("batch_id"), "batch_id", 120)
    content_hash = digest(payload)
    previous = EntityRegistryImport.objects.filter(pk=batch_id).first()
    if previous:
        if previous.content_sha256 != content_hash:
            raise RegistryConflict("同一批次内容已改变，请使用新版本。")
        return {**previous.result, "reused": True, "dry_run": dry_run}
    source = payload.get("source", {})
    if not isinstance(source, dict) or source.get("kind") not in {
        "bootstrap",
        "literature",
    }:
        raise ValueError("source.kind 必须为 bootstrap 或 literature。")
    if (
        source["kind"] == "bootstrap"
        and EntityRegistryImport.objects.filter(source_kind="bootstrap").exists()
    ):
        raise RegistryConflict("基础快照已封存；稳定流程禁止重复拉取或刷新目标库。")
    (
        raw_entities,
        raw_rules,
        raw_mentions,
        raw_classifications,
        raw_updates,
        raw_activations,
    ) = (
        payload.get(k, [])
        for k in (
            "entities",
            "rules",
            "mentions",
            "classifications",
            "rule_updates",
            "new_candidates_activation",
        )
    )
    if any(
        not isinstance(rows, list)
        for rows in [
            raw_entities,
            raw_rules,
            raw_mentions,
            raw_classifications,
            raw_updates,
            raw_activations,
        ]
    ):
        raise ValueError("entities/rules/mentions/classifications 必须为列表。")
    if any(
        len(rows) > 20000
        for rows in [
            raw_entities,
            raw_rules,
            raw_mentions,
            raw_classifications,
            raw_updates,
            raw_activations,
        ]
    ):
        raise ValueError("单批实体登记超过预算。")
    entities = {e.key: e for e in CanonicalEntity.objects.all()}
    seen = set()
    pending = []
    created_entities = 0
    for row in raw_entities:
        if not isinstance(row, dict):
            raise TypeError("实体条目须为对象。")
        key = text(row.get("key"), "实体身份键", 240)
        if key in seen:
            raise ValueError("批次存在重复实体身份键。")
        seen.add(key)
        kind = row.get("kind")
        defaults = {
            "kind": kind,
            "name": text(row.get("name"), "实体名称", 1000),
            "name_en": text(row.get("name_en", ""), "英文名称", 1000, required=False),
            "level": row.get(
                "level", "not_applicable" if kind == "drug" else "unassigned"
            ),
            "status": row.get("status", "candidate"),
            "source_status": row.get("source_status", "literature_new"),
            "external_ids": row.get("external_ids", {}),
            "provenance": row.get("provenance", {}),
        }
        if defaults["status"] not in {"active", "candidate"} or defaults[
            "source_status"
        ] not in {"seed", "literature_new"}:
            raise ValueError("实体状态无效。")
        if any(
            not isinstance(defaults[k], dict) for k in ["external_ids", "provenance"]
        ):
            raise ValueError("实体来源与外部ID须为对象。")
        if kind not in KINDS or defaults["level"] not in LEVELS:
            raise ValueError("实体类型或分类层级无效。")
        entity = entities.get(key)
        if entity:
            if any(getattr(entity, k) != v for k, v in defaults.items()):
                raise RegistryConflict(
                    f"已登记实体 {key} 有不同内容；使用具名版本审核修改。"
                )
        else:
            entity = CanonicalEntity(
                id=uuid.uuid5(NAMESPACE, "entity:" + key), key=key, **defaults
            )
            entities[key] = entity
            created_entities += 1
        pending.append((entity, row.get("main_key"), entity._state.adding))
    # Resolve parents after collecting the whole batch, independent of input ordering.
    for entity, parent_key, adding in pending:
        parent = entities.get(parent_key) if parent_key else None
        if parent_key and parent is None:
            raise ValueError("主癌种身份键未登记。")
        validate_classification(entity.kind, entity.level, parent)
        if not adding and entity.main_cancer_id != (parent.pk if parent else None):
            raise RegistryConflict("已登记的分类不能由批次静默覆盖。")
        entity.main_cancer = parent
    # Parent FKs require main entries to exist before subtype insertion.
    for entity, _, adding in sorted(
        pending, key=lambda item: item[0].level == "subtype"
    ):
        if adding:
            entity.save(force_insert=True)
            _event(
                "entity",
                entity,
                "register",
                "bootstrap" if source["kind"] == "bootstrap" else "literature",
                "本地实体登记，保留来源身份与状态。",
                {},
                entity_snapshot(entity),
                provenance=source,
            )
    if (raw_updates or raw_activations) and source["kind"] != "literature":
        raise ValueError("系统更新仅限literature来源。")
    activation_seen, activated_entities = set(), 0
    for row in raw_activations:
        if not isinstance(row, dict):
            raise TypeError("实体激活条目须为对象。")
        key = text(row.get("entity_key"), "激活实体身份键", 240)
        entity = entities.get(key)
        if entity is None or key in activation_seen:
            raise ValueError("激活实体不存在或本批重复。")
        activation_seen.add(key)
        before = entity_snapshot(entity)
        expected = _system_update_guard("entity", entity, row, before)
        if entity.status != "candidate":
            raise RegistryConflict("系统只能激活未确认候选实体。")
        origin, reason = row.get("origin"), text(row.get("reason"), "激活依据", 10000)
        if origin not in {"rule", "model", "user_instruction"} or not isinstance(
            row.get("evidence", []), list
        ):
            raise ValueError("系统激活来源或证据无效。")
        patch = row.get("provenance_patch", {})
        if not isinstance(patch, dict) or set(patch) - {
            "entity_level",
            "registration_basis",
        }:
            raise ValueError("候选激活仅可补充entity_level/registration_basis元数据。")
        _save_revision(
            entity,
            expected,
            {
                "status": "active",
                "provenance": {
                    **entity.provenance,
                    "activation_origin": origin,
                    "activation_reason": reason,
                    "activation_evidence": row.get("evidence", []),
                    **patch,
                },
            },
        )
        _event(
            "entity",
            entity,
            "system_activate",
            origin,
            reason,
            before,
            entity_snapshot(entity),
            provenance=source,
        )
        activated_entities += 1
    classification_seen, classified_entities = set(), 0
    if raw_classifications and source["kind"] != "literature":
        raise ValueError("后续分类批次仅限literature来源。")
    for row in raw_classifications:
        if not isinstance(row, dict):
            raise TypeError("分类条目须为对象。")
        key = text(row.get("entity_key"), "分类实体身份键", 240)
        if key in classification_seen:
            raise ValueError("同批实体分类重复。")
        classification_seen.add(key)
        entity = entities.get(key)
        if entity is None:
            raise ValueError("分类实体尚未登记。")
        expected = _revision(entity, row)
        origin = row.get("origin")
        reason = text(row.get("reason"), "分类依据", 10000)
        if origin not in {"model", "rule", "user_instruction"}:
            raise ValueError("系统分类来源须为model/rule/user_instruction。")
        if not isinstance(row.get("evidence", []), list):
            raise TypeError("分类证据须为列表。")
        level, parent_key = row.get("level"), row.get("main_key")
        parent = entities.get(parent_key) if parent_key else None
        if parent_key and parent is None:
            raise ValueError("分类主癌种尚未登记。")
        validate_classification(entity.kind, level, parent, entity)
        if EntityRegistryDecision.objects.filter(
            subject_kind="entity", subject_id=entity.pk, origin="human"
        ).exists():
            raise RegistryConflict("系统分类不能覆盖已人工审核实体。")
        same = entity.level == level and entity.main_cancer_id == (
            parent.pk if parent else None
        )
        if entity.level != "unassigned" and not same:
            raise RegistryConflict("系统分类不能覆盖既有主癌种或亚型层级。")
        if same:
            continue
        before = entity_snapshot(entity)
        _save_revision(
            entity,
            expected,
            {
                "level": level,
                "main_cancer": parent,
                "provenance": {
                    **entity.provenance,
                    "classification_assigned": level != "unassigned",
                    "classification_origin": origin,
                    "classification_basis": reason,
                    "classification_evidence": row.get("evidence", []),
                },
            },
        )
        _event(
            "entity",
            entity,
            "set_classification",
            origin,
            reason,
            before,
            entity_snapshot(entity),
            provenance=source,
        )
        classified_entities += 1
    rule_seen, created_rules, updated_rules = set(), 0, 0
    for row, updating in [(r, False) for r in raw_rules] + [
        (r, True) for r in raw_updates
    ]:
        if not isinstance(row, dict):
            raise TypeError("规则条目须为对象。")
        kind, alias = row.get("kind"), text(row.get("alias"), "原名或别名")
        if kind not in KINDS:
            raise ValueError("别名实体类型无效。")
        scope_type, scope_key = scope_data(
            row.get("scope", {"type": "global", "key": ""})
        )
        identity = rule_identity(kind, alias, scope_type, scope_key)
        if identity in rule_seen:
            raise ValueError("同一批次含重复或冲突的同范围别名。")
        rule_seen.add(identity)
        canonical_key = row.get("canonical_key")
        canonical = entities.get(canonical_key) if canonical_key else None
        if canonical_key and canonical is None:
            raise ValueError("别名目标实体未登记。")
        if canonical is not None and canonical.kind != kind:
            raise ValueError("别名不能跨实体类型合并。")
        status, action, origin = (
            row.get("status", "unresolved"),
            row.get("action", "match"),
            row.get("origin"),
        )
        if (
            status not in {"confirmed", "proposed", "unresolved"}
            or action not in {"match", "alias", "new", "ignore"}
            or origin not in ORIGINS
        ):
            raise ValueError("批次规则状态、动作或来源无效。")
        if (
            status == "confirmed"
            and action != "ignore"
            and (canonical is None or canonical.status != "active")
        ):
            raise ValueError("已确认规则必须指向有效的本地实体。")
        if status == "confirmed" and action != "ignore":
            validate_alias_target(alias, canonical)
        if status == "confirmed":
            validate_confirmable_alias(alias)
        defaults = {
            "kind": kind,
            "alias": alias,
            "normalized_alias": normalize(alias),
            "scope_type": scope_type,
            "scope_key": scope_key,
            "canonical_entity": canonical,
            "action": action,
            "status": status,
            "origin": origin,
            "reason": text(row.get("reason"), "规则依据", 10000),
            "alternatives": row.get("alternatives", []),
            "evidence": row.get("evidence", []),
            "provenance": row.get("provenance", {}),
        }
        if (
            not isinstance(defaults["alternatives"], list)
            or not isinstance(defaults["evidence"], list)
            or not isinstance(defaults["provenance"], dict)
        ):
            raise TypeError("规则 alternatives/evidence须为列表，provenance须为对象。")
        if defaults["provenance"].get("match_relation") == "qualified_form":
            qualifiers = defaults["provenance"].get("required_qualifiers")
            if (
                kind != "disease"
                or not isinstance(qualifiers, (dict, list))
                or not qualifiers
            ):
                raise ValueError("限定疾病映射必须保留非空的required_qualifiers。")
        existing = EntityResolutionRule.objects.filter(identity_key=identity).first()
        if updating:
            if existing is None:
                raise RegistryConflict("系统更新的规则尚不存在。")
            before = rule_snapshot(existing)
            expected = _system_update_guard("rule", existing, row, before)
            if existing.status not in {"proposed", "unresolved"}:
                raise RegistryConflict("系统只能更新未确认规则。")
            _save_revision(existing, expected, defaults)
            _event(
                "rule",
                existing,
                "system_update",
                origin,
                existing.reason,
                before,
                rule_snapshot(existing),
                provenance=source,
            )
            updated_rules += 1
        elif existing:
            if any(getattr(existing, k) != v for k, v in defaults.items()):
                raise RegistryConflict(
                    "已登记别名存在不同决策；批次不能覆盖确认或待审规则。"
                )
        else:
            rule = EntityResolutionRule.objects.create(
                id=uuid.uuid5(NAMESPACE, "rule:" + identity),
                identity_key=identity,
                **defaults,
            )
            _event(
                "rule",
                rule,
                "register",
                origin,
                rule.reason,
                {},
                rule_snapshot(rule),
                provenance=source,
            )
            created_rules += 1
    created_mentions, mention_seen = 0, set()
    for row in raw_mentions:
        if not isinstance(row, dict) or not isinstance(row.get("qualifiers", {}), dict):
            raise TypeError("原词引用及qualifiers须为对象。")
        if "rule_qualifiers" in row.get("qualifiers", {}):
            raise ValueError("rule_qualifiers为派生字段，不能从原始批次写入。")
        source_run = text(row.get("source_run"), "source_run", 100)
        group_id = text(row.get("group_id"), "group_id", 80)
        ref = text(row.get("entity_ref"), "entity_ref", 100)
        identity = (source_run, group_id, ref)
        if identity in mention_seen:
            raise ValueError("批次重复引用同一知识实体。")
        mention_seen.add(identity)
        candidate = KnowledgeCandidate.objects.select_related("version").get(
            source_run=source_run, group_id=group_id
        )
        linked = list(
            KnowledgeAnchor.objects.filter(
                candidate=candidate, entity_ref=ref, entity__isnull=False
            ).select_related("entity")[:30]
        )
        entity_ids = {a.entity_id for a in linked}
        if len(entity_ids) != 1:
            raise ValueError("知识原词没有唯一的已有实体及原文定位。")
        original = linked[0].entity
        if original.kind not in KINDS:
            raise ValueError("本轮仅登记药物与癌种。")
        role = "intervention" if original.kind == "drug" else "disease"
        if role not in candidate.fact.get("entity_roles", {}).get(ref, []):
            raise ValueError("不能把背景或对照实体提升为关系参与者。")
        if candidate.version.integrity_status != "valid":
            raise ValueError("来源完整性异常，不能登记别名复用。")
        defaults = {
            "entity": original,
            "kind": original.kind,
            "label": original.label,
            "normalized_label": normalize(original.label),
            "scope_key": str(candidate.version_id),
            "qualifiers": row.get("qualifiers", {}),
        }
        mention, created = EntityRegistryMention.objects.get_or_create(
            candidate=candidate, entity_ref=ref, defaults=defaults
        )
        if not created and (
            any(
                getattr(mention, k) != v
                for k, v in defaults.items()
                if k != "qualifiers"
            )
            or {k: v for k, v in mention.qualifiers.items() if k != "rule_qualifiers"}
            != defaults["qualifiers"]
        ):
            raise RegistryConflict("已登记原词上下文不能由新批次覆盖。")
        created_mentions += int(created)
    resolution = resolve_mentions()
    result = {
        "schema_version": SCHEMA,
        "batch_id": batch_id,
        "reused": False,
        "dry_run": dry_run,
        "created_entities": created_entities,
        "created_rules": created_rules,
        "created_mentions": created_mentions,
        "classified_entities": classified_entities,
        "updated_rules": updated_rules,
        "activated_entities": activated_entities,
        "resolved": resolution,
        "bootstrap_sealed": source["kind"] == "bootstrap"
        or EntityRegistryImport.objects.filter(source_kind="bootstrap").exists(),
        "target_contacted": False,
        "target_written": False,
    }
    EntityRegistryImport.objects.create(
        batch_id=batch_id,
        content_sha256=content_hash,
        source_kind=source["kind"],
        source=source,
        result=result,
    )
    if dry_run:
        transaction.set_rollback(True)
    return result


def _revision(obj, data):
    value = data.get("expected_revision")
    if type(value) is not int or value < 1:
        raise ValueError("必须提供 expected_revision。")
    if obj.revision != value:
        raise RegistryConflict("记录已更新，请刷新后核对。")
    return value


def _system_update_guard(subject_kind, obj, data, before):
    expected = _revision(obj, data)
    if data.get("expected_content_sha256") != digest(before):
        raise RegistryConflict("系统更新前的内容哈希不匹配。")
    if (
        getattr(obj, "origin", None) == "human"
        or EntityRegistryDecision.objects.filter(
            subject_kind=subject_kind, subject_id=obj.pk, origin="human"
        ).exists()
    ):
        raise RegistryConflict("系统更新不能覆盖人工审核。")
    return expected


def _save_revision(obj, expected, changes):
    changed = (
        type(obj)
        .objects.filter(pk=obj.pk, revision=expected)
        .update(**changes, revision=F("revision") + 1, updated_at=timezone.now())
    )
    if changed != 1:
        raise RegistryConflict("记录已更新，请刷新后核对。")
    obj.refresh_from_db()


@transaction.atomic
def review_rule(rule_id, data, actor):
    rule = EntityResolutionRule.objects.get(pk=rule_id)
    expected = _revision(rule, data)
    before = rule_snapshot(rule)
    action, reason = data.get("action"), text(data.get("reason"), "审核理由", 10000)
    if action not in {"confirm", "defer", "reject", "revoke", "create"}:
        raise ValueError("不支持的规则审核动作。")
    changes = {"origin": "human", "reason": reason}
    if action in {"confirm", "create"}:
        validate_confirmable_alias(rule.alias)
        canonical = rule.canonical_entity
        if action == "create":
            name = text(data.get("name"), "新实体名称", 1000)
            if CanonicalEntity.objects.filter(
                kind=rule.kind, name__iexact=name
            ).exists():
                raise RegistryConflict("已有同名实体，请先核对并选择已有词条。")
            main = (
                CanonicalEntity.objects.get(pk=data["main_id"])
                if data.get("main_id")
                else None
            )
            level = data.get(
                "level", "not_applicable" if rule.kind == "drug" else "unassigned"
            )
            validate_classification(rule.kind, level, main)
            canonical = CanonicalEntity.objects.create(
                key="local:" + str(uuid.uuid4()),
                kind=rule.kind,
                name=name,
                name_en=text(data.get("name_en", ""), "英文名称", 1000, required=False),
                level=level,
                main_cancer=main,
                status="active",
                source_status="literature_new",
                provenance={
                    "rule_id": str(rule.pk),
                    "registration": "human",
                    "reason": reason,
                },
            )
            _event(
                "entity",
                canonical,
                "register",
                "human",
                reason,
                {},
                entity_snapshot(canonical),
                actor=actor,
            )
        elif data.get("canonical_id"):
            canonical = CanonicalEntity.objects.get(pk=data["canonical_id"])
        if rule.action != "ignore" and (
            canonical is None
            or canonical.kind != rule.kind
            or canonical.status != "active"
        ):
            raise ValueError("请选择同类型的有效实体，或先确认新增候选实体。")
        if rule.action != "ignore":
            validate_alias_target(rule.alias, canonical)
        changes.update(status="confirmed", canonical_entity=canonical)
        if action == "create":
            changes["action"] = "new"
    else:
        changes["status"] = {
            "defer": "unresolved",
            "reject": "rejected",
            "revoke": "revoked",
        }[action]
    _save_revision(rule, expected, changes)
    _event(
        "rule", rule, action, "human", reason, before, rule_snapshot(rule), actor=actor
    )
    resolution = resolve_mentions(
        kind=rule.kind, normalized_alias=rule.normalized_alias
    )
    return rule, resolution


@transaction.atomic
def quarantine_placeholder_rules():
    """Retire unconfirmed source punctuation; never rewrite sealed inputs or names."""
    rejected = []
    affected = changed = 0
    candidates = EntityResolutionRule.objects.filter(
        status__in=["unresolved", "proposed"]
    ).exclude(origin="human")
    for rule in candidates:
        if any(character.isalnum() for character in rule.alias):
            continue
        if EntityRegistryDecision.objects.filter(
            subject_kind="rule", subject_id=rule.pk, origin="human"
        ).exists():
            continue
        before = rule_snapshot(rule)
        reason = (
            "来源占位符仅含标点，不能用于实体身份识别；保留源字段与原实体，不建立别名。"
        )
        _save_revision(
            rule,
            rule.revision,
            {
                "status": "rejected",
                "origin": "rule",
                "reason": reason,
                "provenance": {
                    **rule.provenance,
                    "source_placeholder_quarantined": True,
                },
            },
        )
        _event(
            "rule",
            rule,
            "quarantine_placeholder",
            "rule",
            reason,
            before,
            rule_snapshot(rule),
        )
        resolution = resolve_mentions(
            kind=rule.kind, normalized_alias=rule.normalized_alias
        )
        affected += resolution["affected"]
        changed += resolution["changed"]
        rejected.append(str(rule.pk))
    return {
        "rejected": len(rejected),
        "rule_ids": rejected,
        "affected_mentions": affected,
        "changed_mentions": changed,
    }


@transaction.atomic
def review_entity(entity_id, data, actor):
    entity = CanonicalEntity.objects.get(pk=entity_id)
    expected = _revision(entity, data)
    before = entity_snapshot(entity)
    action, reason = data.get("action"), text(data.get("reason"), "审核理由", 10000)
    changes = {}
    if action == "set_classification":
        main = (
            CanonicalEntity.objects.get(pk=data["main_id"])
            if data.get("main_id")
            else None
        )
        level = data.get("level")
        validate_classification(entity.kind, level, main, entity)
        changes.update(level=level, main_cancer=main)
        changes["provenance"] = {
            **entity.provenance,
            "classification_assigned": level != "unassigned",
            "classification_origin": "human",
            "classification_basis": reason,
        }
    elif action == "rename":
        changes.update(
            name=text(data.get("name"), "实体名称", 1000),
            name_en=text(
                data.get("name_en", entity.name_en), "英文名称", 1000, required=False
            ),
        )
    elif action in {"activate", "defer"}:
        changes["status"] = "active" if action == "activate" else "candidate"
    else:
        raise ValueError("不支持的实体审核动作。")
    _save_revision(entity, expected, changes)
    _event(
        "entity",
        entity,
        action,
        "human",
        reason,
        before,
        entity_snapshot(entity),
        actor=actor,
    )
    resolution = resolve_mentions(kind=entity.kind)
    return entity, resolution
