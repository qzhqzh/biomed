# ruff: noqa: RUF012
"""Local, source-anchored research candidates; separate from published Evidence."""

import uuid

from django.conf import settings
from django.db import models


class KnowledgeEntity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=64, unique=True)
    scope_version = models.ForeignKey(
        "curation.MaterialVersion",
        on_delete=models.PROTECT,
        related_name="knowledge_entities",
    )
    kind = models.CharField(max_length=32, db_index=True)
    subtype = models.CharField(max_length=64, blank=True)
    label = models.CharField(max_length=2000)
    status = models.CharField(max_length=24, default="candidate")
    attributes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "label", "id"]


class KnowledgeCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_run = models.CharField(max_length=100, db_index=True)
    group_id = models.CharField(max_length=80)
    version = models.ForeignKey(
        "curation.MaterialVersion",
        on_delete=models.PROTECT,
        related_name="knowledge_candidates",
    )
    content_sha256 = models.CharField(max_length=64)
    import_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=32, default="research_candidate")
    ai_status = models.CharField(max_length=16, default="pending")
    human_status = models.CharField(max_length=16, default="unchecked")
    publication_status = models.CharField(max_length=16, default="draft", db_index=True)
    published_at = models.DateTimeField(null=True)
    current_review = models.ForeignKey(
        "curation.KnowledgeReview",
        null=True,
        on_delete=models.PROTECT,
        related_name="current_candidates",
    )
    fact = models.JSONField(default=dict)
    assessment = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["source_run", "group_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_run", "group_id"], name="unique_knowledge_run_group"
            ),
            models.CheckConstraint(
                condition=~models.Q(publication_status="published")
                | (
                    models.Q(ai_status="pass", current_review__isnull=False)
                    & ~models.Q(human_status="flagged")
                ),
                name="knowledge_published_requires_ai",
            ),
        ]


class KnowledgeAnchor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        KnowledgeCandidate, on_delete=models.PROTECT, related_name="anchors"
    )
    entity = models.ForeignKey(
        KnowledgeEntity, null=True, on_delete=models.PROTECT, related_name="anchors"
    )
    passage = models.ForeignKey(
        "curation.Passage", on_delete=models.PROTECT, related_name="knowledge_anchors"
    )
    content_sha256 = models.CharField(max_length=64)
    entity_ref = models.CharField(max_length=100, blank=True)
    purpose = models.CharField(max_length=24)
    association = models.CharField(max_length=24)
    roles = models.JSONField(default=list)
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    quote = models.TextField()
    attributes = models.JSONField(default=dict)

    class Meta:
        ordering = ["candidate__group_id", "passage__ordinal", "start", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "content_sha256"], name="unique_knowledge_anchor"
            ),
            models.CheckConstraint(
                condition=models.Q(end__gt=models.F("start")),
                name="knowledge_anchor_nonempty",
            ),
        ]


class ImmutableReviewQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Knowledge reviews are immutable")

    def delete(self):
        raise ValueError("Knowledge reviews are immutable")


class KnowledgeReview(models.Model):
    """Append-only review event, independent of extraction research assessment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        KnowledgeCandidate, on_delete=models.PROTECT, related_name="reviews"
    )
    review_key = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16)
    decision = models.CharField(max_length=16)
    reason = models.TextField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    provider = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    policy_version = models.CharField(max_length=100)
    review_run = models.CharField(max_length=100)
    input_sha256 = models.CharField(max_length=64)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ImmutableReviewQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Knowledge reviews are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Knowledge reviews are immutable")


class KnowledgeSearchTerm(models.Model):
    """Only relationship terms explicitly approved in the referenced AI review."""

    candidate = models.ForeignKey(
        KnowledgeCandidate, on_delete=models.PROTECT, related_name="search_terms"
    )
    review = models.ForeignKey(
        KnowledgeReview, on_delete=models.PROTECT, related_name="terms"
    )
    entity = models.ForeignKey(KnowledgeEntity, on_delete=models.PROTECT)
    role = models.CharField(max_length=24)
    term = models.CharField(max_length=2000)
    normalized_term = models.CharField(max_length=2000)
    basis = models.CharField(max_length=2000)

    class Meta:
        indexes = [
            models.Index(
                fields=["role", "normalized_term"], name="knowledge_term_lookup"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["review", "entity", "role", "normalized_term"],
                name="unique_review_entity_term",
            )
        ]
