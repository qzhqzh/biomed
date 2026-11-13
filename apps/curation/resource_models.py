# ruff: noqa: RUF012
"""Additive, versioned reference resources; these are not clinical Evidence."""

import uuid

from django.db import models
from django.db.models import Q


class ResourceDataset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    namespace = models.CharField(max_length=24)
    version = models.CharField(max_length=100)
    source_url = models.URLField(max_length=1000)
    rights = models.JSONField(default=dict)
    manifest_sha256 = models.CharField(max_length=64)
    manifest = models.JSONField()
    counts = models.JSONField(default=dict)
    active = models.BooleanField(default=False)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "manifest_sha256"], name="unique_resource_snapshot"
            ),
            models.UniqueConstraint(
                fields=["namespace"],
                condition=Q(active=True),
                name="one_active_resource_dataset",
            ),
        ]


class ResourceEntity(models.Model):
    dataset = models.ForeignKey(
        ResourceDataset, on_delete=models.PROTECT, related_name="entities"
    )
    kind = models.CharField(max_length=24)
    identifier = models.CharField(max_length=80)
    label = models.CharField(max_length=1000)
    attributes = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "kind", "identifier"], name="unique_resource_entity"
            )
        ]


class ResourceAssertion(models.Model):
    dataset = models.ForeignKey(
        ResourceDataset, on_delete=models.PROTECT, related_name="assertions"
    )
    kind = models.CharField(max_length=24)
    source_record_id = models.CharField(max_length=160)
    subject_identifier = models.CharField(max_length=80)
    object_identifier = models.CharField(max_length=80, blank=True)
    predicate = models.CharField(max_length=100, blank=True)
    attributes = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "kind", "source_record_id"],
                name="unique_resource_assertion",
            )
        ]
        indexes = [
            models.Index(
                fields=["dataset", "kind", "subject_identifier"],
                name="resource_subject_lookup",
            )
        ]


class DrugMoleculeLink(models.Model):
    dataset = models.ForeignKey(
        ResourceDataset, on_delete=models.PROTECT, related_name="drug_links"
    )
    ingredient = models.ForeignKey(
        "curation.Concept", on_delete=models.PROTECT, related_name="molecule_links"
    )
    molecule_identifier = models.CharField(max_length=80)
    method = models.CharField(max_length=100)
    status = models.CharField(max_length=24, default="candidate")
    provenance = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "ingredient", "molecule_identifier"],
                name="unique_drug_molecule_link",
            )
        ]


class EntityTranslation(models.Model):
    concept = models.ForeignKey(
        "curation.Concept", on_delete=models.PROTECT, related_name="translations"
    )
    language = models.CharField(max_length=16, default="zh-Hans")
    label = models.CharField(max_length=1000)
    provider = models.CharField(max_length=100)
    status = models.CharField(max_length=24)
    source_url = models.URLField(max_length=1000, blank=True)
    source_version = models.CharField(max_length=100)
    match_basis = models.CharField(max_length=200)
    content_sha256 = models.CharField(max_length=64)
    provenance = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["concept", "content_sha256"], name="unique_concept_translation"
            )
        ]
