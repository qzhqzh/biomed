from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("curation", "0007_identifier_lookup_indexes"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="collectionpartition",
            name="unique_collection_partition",
        ),
        migrations.AddField(
            model_name="collectionpartition",
            name="uid_start",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="collectionpartition",
            name="uid_end",
            field=models.PositiveBigIntegerField(default=9999999999),
        ),
        migrations.AddConstraint(
            model_name="collectionpartition",
            constraint=models.UniqueConstraint(
                fields=("collection", "start_date", "end_date", "uid_start", "uid_end"),
                name="unique_collection_partition",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionpartition",
            constraint=models.CheckConstraint(
                condition=models.Q(uid_start__gte=1, uid_end__lte=9999999999,
                    uid_start__lte=models.F("uid_end")),
                name="collection_partition_uid_range",
            ),
        ),
    ]
