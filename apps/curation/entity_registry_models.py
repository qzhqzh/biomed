# ruff: noqa: RUF012
"""Reusable local entity identities; original literature mentions remain immutable."""

import uuid

from django.conf import settings
from django.db import models


class CanonicalEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=240, unique=True)
    kind = models.CharField(max_length=24, db_index=True)
    name = models.CharField(max_length=1000)
    name_en = models.CharField(max_length=1000, blank=True)
    level = models.CharField(max_length=24, default="unassigned", db_index=True)
    main_cancer = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="subtypes"
    )
    status = models.CharField(max_length=24, default="candidate", db_index=True)
    source_status = models.CharField(
        max_length=24, default="literature_new", db_index=True
    )
    external_ids = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(main_cancer__isnull=True)
                | models.Q(kind="disease", level="subtype"),
                name="registry_parent_requires_subtype",
            )
        ]


class EntityResolutionRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=24, db_index=True)
    alias = models.CharField(max_length=2000)
    normalized_alias = models.CharField(max_length=2000, db_index=True)
    scope_type = models.CharField(max_length=24, default="global")
    scope_key = models.CharField(max_length=100, blank=True)
    canonical_entity = models.ForeignKey(
        CanonicalEntity, null=True, on_delete=models.PROTECT, related_name="rules"
    )
    action = models.CharField(max_length=24, default="match")
    status = models.CharField(max_length=24, default="unresolved", db_index=True)
    origin = models.CharField(max_length=32)
    reason = models.TextField()
    alternatives = models.JSONField(default=list)
    evidence = models.JSONField(default=list)
    provenance = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "kind", "alias", "id"]


class EntityRegistryMention(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        "curation.KnowledgeCandidate",
        on_delete=models.PROTECT,
        related_name="registry_mentions",
    )
    entity = models.ForeignKey(
        "curation.KnowledgeEntity",
        on_delete=models.PROTECT,
        related_name="registry_mentions",
    )
    entity_ref = models.CharField(max_length=100)
    kind = models.CharField(max_length=24)
    label = models.CharField(max_length=2000)
    normalized_label = models.CharField(max_length=2000, db_index=True)
    scope_key = models.CharField(max_length=100)
    qualifiers = models.JSONField(default=dict)
    canonical_entity = models.ForeignKey(
        CanonicalEntity, null=True, on_delete=models.PROTECT, related_name="mentions"
    )
    applied_rule = models.ForeignKey(
        EntityResolutionRule,
        null=True,
        on_delete=models.PROTECT,
        related_name="mentions",
    )
    state = models.CharField(max_length=24, default="unresolved", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["candidate__source_run", "candidate__group_id", "entity_ref"]
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "entity_ref"], name="unique_registry_mention"
            )
        ]


class RegistryDecisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Entity registry decisions are immutable")

    def delete(self):
        raise ValueError("Entity registry decisions are immutable")


class EntityRegistryDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_key = models.CharField(max_length=64, unique=True)
    subject_kind = models.CharField(max_length=24)
    subject_id = models.UUIDField()
    action = models.CharField(max_length=32)
    origin = models.CharField(max_length=32)
    reason = models.TextField()
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    objects = RegistryDecisionQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["subject_kind", "subject_id"], name="registry_decision_subject"
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Entity registry decisions are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Entity registry decisions are immutable")


class EntityRegistryImport(models.Model):
    batch_id = models.CharField(max_length=120, primary_key=True)
    content_sha256 = models.CharField(max_length=64)
    source_kind = models.CharField(max_length=24, db_index=True)
    source = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
