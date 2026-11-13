# ruff: noqa: RUF012
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("curation", "0010_knowledge_candidates"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KnowledgeSearchTerm",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("role", models.CharField(max_length=24)),
                ("term", models.CharField(max_length=2000)),
                ("normalized_term", models.CharField(max_length=2000)),
                ("basis", models.CharField(max_length=2000)),
            ],
        ),
        migrations.AddField(
            model_name="knowledgecandidate",
            name="ai_status",
            field=models.CharField(default="pending", max_length=16),
        ),
        migrations.AddField(
            model_name="knowledgecandidate",
            name="human_status",
            field=models.CharField(default="unchecked", max_length=16),
        ),
        migrations.AddField(
            model_name="knowledgecandidate",
            name="publication_status",
            field=models.CharField(db_index=True, default="draft", max_length=16),
        ),
        migrations.AddField(
            model_name="knowledgecandidate",
            name="published_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.CreateModel(
            name="KnowledgeReview",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("review_key", models.CharField(max_length=64, unique=True)),
                ("kind", models.CharField(max_length=16)),
                ("decision", models.CharField(max_length=16)),
                ("reason", models.TextField()),
                ("provider", models.CharField(blank=True, max_length=100)),
                ("model", models.CharField(blank=True, max_length=100)),
                ("policy_version", models.CharField(max_length=100)),
                ("review_run", models.CharField(max_length=100)),
                ("input_sha256", models.CharField(max_length=64)),
                ("input_snapshot", models.JSONField(default=dict)),
                ("result", models.JSONField(default=dict)),
                ("provenance", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviews",
                        to="curation.knowledgecandidate",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddField(
            model_name="knowledgecandidate",
            name="current_review",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_candidates",
                to="curation.knowledgereview",
            ),
        ),
        migrations.AddConstraint(
            model_name="knowledgecandidate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("publication_status", "published"), _negated=True),
                    models.Q(
                        ("ai_status", "pass"),
                        ("current_review__isnull", False),
                        models.Q(("human_status", "flagged"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="knowledge_published_requires_ai",
            ),
        ),
        migrations.AddField(
            model_name="knowledgesearchterm",
            name="candidate",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="search_terms",
                to="curation.knowledgecandidate",
            ),
        ),
        migrations.AddField(
            model_name="knowledgesearchterm",
            name="entity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="curation.knowledgeentity",
            ),
        ),
        migrations.AddField(
            model_name="knowledgesearchterm",
            name="review",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="terms",
                to="curation.knowledgereview",
            ),
        ),
        migrations.AddIndex(
            model_name="knowledgesearchterm",
            index=models.Index(
                fields=["role", "normalized_term"], name="knowledge_term_lookup"
            ),
        ),
        migrations.AddConstraint(
            model_name="knowledgesearchterm",
            constraint=models.UniqueConstraint(
                fields=("review", "entity", "role", "normalized_term"),
                name="unique_review_entity_term",
            ),
        ),
    ]
