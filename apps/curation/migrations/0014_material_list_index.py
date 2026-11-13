# Django declares migration dependencies and operations as class-level lists.
# ruff: noqa: RUF012
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("curation", "0013_collection_hotpath_indexes")]

    operations = [
        migrations.AddIndex(
            model_name="material",
            index=models.Index(
                fields=["-created_at", "kind", "id"], name="material_list_created"
            ),
        ),
    ]
