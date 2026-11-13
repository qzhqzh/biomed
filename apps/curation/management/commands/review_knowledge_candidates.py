"""Validate a controlled file of actual model reviews and append publication events."""

import json
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.curation.knowledge_publication import apply_review_batch


class Command(BaseCommand):
    help = "Apply independently generated model reviews; never synthesize AI decisions."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--quality-only", action="store_true",
            help="Append independent quality results without changing publication or extraction.",
        )

    def handle(self, *args, **options):
        try:
            payload = json.loads(options["input"].read_text(encoding="utf-8"))
            result = apply_review_batch(
                payload, dry_run=options["dry_run"], publish=not options["quality_only"]
            )
        except (
            ValueError,
            TypeError,
            OSError,
            KeyError,
            ObjectDoesNotExist,
            ValidationError,
        ) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result, ensure_ascii=False))
