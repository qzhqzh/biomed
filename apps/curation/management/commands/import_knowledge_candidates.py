"""Append already extracted, precisely anchored research candidates."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.curation.knowledge_storage import import_candidates


class Command(BaseCommand):
    help = "Validate and import research knowledge candidates; no inference or publication."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            payload = json.loads(options["input"].read_text(encoding="utf-8"))
            result = import_candidates(payload, dry_run=options["dry_run"])
        except (ValueError, TypeError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result, ensure_ascii=False))
