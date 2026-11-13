# ruff: noqa: RUF012
# Django Meta and migration declarations are intentionally class-level lists.
"""Versioned sources, anchored evidence and durable work. Never store demo evidence here."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.curation.db import LowerIdentifier
from apps.curation.entity_registry_models import (  # noqa: F401
    CanonicalEntity,
    EntityRegistryDecision,
    EntityRegistryImport,
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import (  # noqa: F401
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    KnowledgeReview,
    KnowledgeSearchTerm,
)
from apps.curation.resource_models import (  # noqa: F401
    DrugMoleculeLink,
    EntityTranslation,
    ResourceAssertion,
    ResourceDataset,
    ResourceEntity,
)


class SourceRelease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    namespace = models.CharField(max_length=24)
    version = models.CharField(max_length=100)
    source_url = models.URLField(max_length=500)
    rights = models.TextField()
    sha256 = models.CharField(max_length=64)
    count = models.PositiveIntegerField()
    active = models.BooleanField(default=False)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "sha256"], name="unique_source_content"
            ),
            models.UniqueConstraint(
                fields=["namespace"],
                condition=Q(active=True),
                name="one_active_source_release",
            ),
        ]


class Concept(models.Model):
    release = models.ForeignKey(
        SourceRelease, on_delete=models.PROTECT, related_name="concepts"
    )
    identifier = models.CharField(max_length=80)
    label = models.CharField(max_length=700)
    synonyms = models.JSONField(default=list)
    cross_references = models.JSONField(default=list)
    parents = models.JSONField(default=list)
    definition = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["release", "identifier"], name="concept_per_release"
            )
        ]
        indexes = [models.Index(fields=["release", "label"])]


class Material(models.Model):
    class Kind(models.TextChoices):
        PAPER = "paper", "论文"
        GUIDELINE = "guideline", "指南"
        OTHER = "other", "其他文本"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_key = models.CharField(max_length=180, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PAPER)
    title = models.CharField(max_length=2000)
    identifiers = models.JSONField(default=dict)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(LowerIdentifier("doi"), name="curation_doi_lookup"),
            models.Index(LowerIdentifier("pmc"), name="curation_pmc_lookup"),
            models.Index(fields=["-created_at", "kind", "id"], name="material_list_created"),
        ]


class MaterialVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="versions"
    )
    sha256 = models.CharField(max_length=64)
    media_type = models.CharField(max_length=80)
    filename = models.CharField(max_length=255)
    source_url = models.URLField(max_length=1000, blank=True)
    rights = models.TextField()
    acquired_at = models.DateTimeField(auto_now_add=True)
    integrity_status = models.CharField(max_length=20, default="valid")
    integrity_note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["material", "sha256"], name="unique_material_version"
            )
        ]


class Passage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        MaterialVersion, on_delete=models.PROTECT, related_name="passages"
    )
    parser_version = models.CharField(max_length=80)
    ordinal = models.PositiveIntegerField()
    locator = models.CharField(max_length=240)
    text = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "parser_version", "ordinal"],
                name="unique_source_passage",
            )
        ]
        ordering = ["ordinal"]


class WorkItem(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUCCEEDED = "succeeded", "完成"
        FAILED = "failed", "失败"
        BLOCKED = "blocked", "待处理"
        WAITING = "waiting_configuration", "待配置模型"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=40)
    dedup_key = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=24, choices=State.choices, default=State.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    result = models.JSONField(default=dict)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "available_at"])]
        ordering = ["-created_at"]


class WorkAttempt(models.Model):
    job = models.ForeignKey(WorkItem, on_delete=models.PROTECT, related_name="runs")
    number = models.PositiveIntegerField()
    token = models.UUIDField(unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    outcome = models.CharField(max_length=24, default="running")
    message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "number"], name="unique_job_attempt")
        ]


class WorkerHeartbeat(models.Model):
    name = models.CharField(max_length=80, primary_key=True)
    last_seen = models.DateTimeField(default=timezone.now)


class Evidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Each candidate is an immutable assertion against immutable source passages.
    extraction = models.ForeignKey(
        WorkItem, on_delete=models.PROTECT, related_name="candidates"
    )
    ordinal = models.PositiveIntegerField()
    passage = models.ForeignKey(
        Passage, on_delete=models.PROTECT, related_name="evidence"
    )
    quote = models.TextField()
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    assertion = models.TextField()
    context = models.JSONField(default=dict)
    entity_links = models.JSONField(default=list)
    ai_status = models.CharField(max_length=16, default="pending")
    human_status = models.CharField(max_length=16, default="unchecked")
    publication_status = models.CharField(max_length=16, default="draft", db_index=True)
    published_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["extraction", "ordinal"], name="unique_extracted_candidate"
            ),
            models.CheckConstraint(
                condition=~Q(publication_status="published") | Q(ai_status="pass"),
                name="published_requires_ai_pass",
            ),
            models.CheckConstraint(
                condition=~Q(human_status="flagged")
                | Q(publication_status="withdrawn"),
                name="flagged_evidence_is_withdrawn",
            ),
        ]
        ordering = ["-created_at"]


class Review(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evidence = models.ForeignKey(
        Evidence, on_delete=models.PROTECT, related_name="reviews"
    )
    kind = models.CharField(max_length=16)  # ai / human
    decision = models.CharField(max_length=16)  # pass / fail / confirm / flag
    reason = models.TextField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    job = models.OneToOneField(WorkItem, null=True, on_delete=models.PROTECT)
    provenance = models.JSONField(
        default=dict
    )  # backend, model, prompt, policy versions
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class LiteratureCollection(models.Model):
    """A bounded collection wave; raising limits is an explicit resume operation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slot = models.PositiveSmallIntegerField(default=1, editable=False)
    start_date = models.DateField()
    end_date = models.DateField()
    query = models.TextField()
    taxonomy = models.JSONField()
    limits = models.JSONField()
    state = models.CharField(max_length=20, default="estimating")
    phase = models.CharField(max_length=20, default="estimate")
    step = models.PositiveIntegerField(default=0)
    estimate = models.JSONField(default=dict)
    message = models.TextField(blank=True)
    consecutive_errors = models.PositiveSmallIntegerField(default=0)
    retry_not_before = models.DateTimeField(null=True)
    active_seconds = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(slot=1), name="collection_single_slot"),
            models.UniqueConstraint(
                fields=["slot"],
                condition=Q(state__in=["estimating", "running"]),
                name="one_active_collection",
            ),
        ]


class CollectionAlert(models.Model):
    """Durable, deduplicated attention events; resuming resolves without deleting."""

    collection = models.ForeignKey(
        LiteratureCollection, on_delete=models.PROTECT, related_name="alerts"
    )
    step = models.PositiveIntegerField()
    reason = models.CharField(max_length=40)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, db_index=True)
    email_state = models.CharField(max_length=24, default="pending", db_index=True)
    email_attempts = models.PositiveSmallIntegerField(default=0)
    email_available_at = models.DateTimeField(null=True)
    email_sent_at = models.DateTimeField(null=True)
    email_message_id = models.CharField(max_length=255, blank=True)
    email_error = models.TextField(blank=True)
    delivery_token = models.UUIDField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "step"], name="unique_collection_step_alert"
            )
        ]


class CollectionPartition(models.Model):
    collection = models.ForeignKey(
        LiteratureCollection, on_delete=models.PROTECT, related_name="partitions"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    uid_start = models.PositiveBigIntegerField(default=1)
    uid_end = models.PositiveBigIntegerField(default=9999999999)
    state = models.CharField(max_length=16, default="pending")
    count = models.PositiveIntegerField(null=True)
    pmids = models.JSONField(default=list)
    offset = models.PositiveIntegerField(default=0)
    captured_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "start_date", "end_date", "uid_start", "uid_end"],
                name="unique_collection_partition",
            ),
            models.CheckConstraint(
                condition=models.Q(uid_start__gte=1, uid_end__lte=9999999999,
                    uid_start__lte=models.F("uid_end")),
                name="collection_partition_uid_range",
            ),
        ]


class CollectionEntry(models.Model):
    collection = models.ForeignKey(
        LiteratureCollection, on_delete=models.PROTECT, related_name="entries"
    )
    pmid = models.CharField(max_length=10)
    identifiers = models.JSONField(default=dict)
    metadata_version = models.ForeignKey(
        MaterialVersion,
        null=True,
        on_delete=models.PROTECT,
        related_name="collection_entries",
    )
    reused = models.BooleanField(default=False)
    fulltext_state = models.CharField(max_length=24, default="pending")
    fulltext_version = models.ForeignKey(
        MaterialVersion,
        null=True,
        on_delete=models.PROTECT,
        related_name="collection_fulltexts",
    )
    message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["collection", "fulltext_state", "id", "reused", "metadata_version"],
                name="collect_entry_fulltext",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "pmid"], name="unique_collection_pmid"
            )
        ]
        ordering = ["id"]


class LiteratureClassification(models.Model):
    version = models.ForeignKey(
        MaterialVersion, on_delete=models.PROTECT, related_name="classifications"
    )
    taxonomy_version = models.CharField(max_length=80)
    taxonomy_sha256 = models.CharField(max_length=64)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "taxonomy_sha256"],
                name="unique_literature_classification",
            )
        ]


class CollectionFetch(models.Model):
    """Reserve response budgets before I/O; crashed requests retain their reservation."""

    collection = models.ForeignKey(
        LiteratureCollection, on_delete=models.PROTECT, related_name="fetches"
    )
    url = models.TextField()
    state = models.CharField(max_length=16, default="running")
    http_status = models.PositiveSmallIntegerField(null=True)
    charged_bytes = models.PositiveBigIntegerField()
    wire_bytes = models.PositiveBigIntegerField(default=0)
    decoded_bytes = models.PositiveBigIntegerField(default=0)
    elapsed_seconds = models.FloatField(default=0)
    response_sha256 = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["collection", "charged_bytes", "wire_bytes", "decoded_bytes", "elapsed_seconds", "state"],
                name="collect_fetch_budget",
            ),
            models.Index(fields=["started_at"], name="collect_fetch_started"),
        ]
