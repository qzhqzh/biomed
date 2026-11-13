"""Import an explicitly prepared local registry batch; never fetch target data."""

import json
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from apps.curation.entity_registry import import_registry


class Command(BaseCommand):
    help = "Import a frozen local entity registry batch; bootstrap is sealed after first import."

    def add_arguments(self, parser):
        parser.add_argument("--input", type=Path, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            result = import_registry(
                json.loads(options["input"].read_text()), dry_run=options["dry_run"]
            )
        except (ValueError, TypeError, OSError, ObjectDoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
