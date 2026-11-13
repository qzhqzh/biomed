"""Append a rule revision from immutable metadata; never download or call a model."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.curation.classification import (
    DEFAULT_TAXONOMY,
    TAXONOMIES,
    classify,
    taxonomy_hash,
    taxonomy_snapshot,
)
from apps.curation.materials import pubmed_metadata, pubmed_record_id, pubmed_records
from apps.curation.models import (
    LiteratureClassification,
    LiteratureCollection,
    WorkItem,
)
from apps.curation.network import SourceBlocked, xml
from apps.curation.storage import read_blob


def check_stopped(run):
    if (
        run.state not in ("paused", "complete", "ready")
        or WorkItem.objects.filter(
            kind="collect_literature",
            payload__collection_id=str(run.pk),
            state__in=("queued", "running"),
        ).exists()
    ):
        raise SourceBlocked("请先暂停采集并等待当前步骤保存后，再追加分类版本")


def reclassify_collection(pk, taxonomy):
    run = LiteratureCollection.objects.get(pk=pk)
    check_stopped(run)
    digest = taxonomy_hash(taxonomy)
    entries = list(
        run.entries.filter(metadata_version__isnull=False).select_related(
            "metadata_version"
        )
    )
    reused = created = 0
    for offset in range(0, len(entries), 50):
        chunk = entries[offset : offset + 50]
        existing = set(
            LiteratureClassification.objects.filter(
                version_id__in=[entry.metadata_version_id for entry in chunk],
                taxonomy_sha256=digest,
            ).values_list("version_id", flat=True)
        )
        pending = []
        for entry in chunk:
            version = entry.metadata_version
            if version.pk in existing:
                reused += 1
                continue
            if version.integrity_status != "valid":
                raise SourceBlocked(f"PMID {entry.pmid} 的来源版本完整性尚未通过")
            records = pubmed_records(xml(read_blob(version.sha256)))
            if len(records) != 1 or pubmed_record_id(records[0]) != entry.pmid:
                raise SourceBlocked(f"PMID {entry.pmid} 的原始元数据身份不匹配")
            pending.append((version, pubmed_metadata(records[0])))
        if not pending:
            continue
        with transaction.atomic():
            # Bound SQLite write transactions and retain completed chunks after a stop.
            check_stopped(LiteratureCollection.objects.select_for_update().get(pk=pk))
            existing = set(
                LiteratureClassification.objects.filter(
                    version_id__in=[version.pk for version, _ in pending],
                    taxonomy_sha256=digest,
                ).values_list("version_id", flat=True)
            )
            for version, metadata in pending:
                if version.pk in existing:
                    reused += 1
                else:
                    classify(version, metadata, taxonomy)
                    created += 1
    return {
        "collection_id": str(run.pk),
        "taxonomy_version": taxonomy["version"],
        "taxonomy_sha256": digest,
        "classified": len(entries),
        "created": created,
        "reused": reused,
        "downloaded": 0,
        "model_calls": 0,
    }


class Command(BaseCommand):
    help = "从已保存的元数据追加分类版本；保留旧结果，不下载、不训练"

    def add_arguments(self, parser):
        parser.add_argument("collection_id")
        parser.add_argument(
            "--taxonomy", choices=list(TAXONOMIES), default=DEFAULT_TAXONOMY
        )

    def handle(self, *args, **options):
        try:
            result = reclassify_collection(
                options["collection_id"], taxonomy_snapshot(options["taxonomy"])
            )
        except (SourceBlocked, ValueError, LiteratureCollection.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
