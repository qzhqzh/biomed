# Django declares migration dependencies and operations as class-level lists.
# ruff: noqa: RUF012
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("curation", "0012_entity_registry")]

    operations = [
        migrations.AddIndex(
            model_name="collectionentry",
            index=models.Index(
                fields=["collection", "fulltext_state", "id", "reused", "metadata_version"],
                name="collect_entry_fulltext",
            ),
        ),
        migrations.AddIndex(
            model_name="collectionfetch",
            index=models.Index(
                fields=["collection", "charged_bytes", "wire_bytes", "decoded_bytes", "elapsed_seconds", "state"],
                name="collect_fetch_budget",
            ),
        ),
        migrations.AddIndex(
            model_name="collectionfetch",
            index=models.Index(fields=["started_at"], name="collect_fetch_started"),
        ),
    ]
