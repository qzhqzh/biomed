import json

from django.core.management.base import BaseCommand, CommandError

from apps.curation.reference_resources import import_snapshot, import_translations


class Command(BaseCommand):
    help = "Validate and import a completed reference snapshot or bilingual overlay"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--translations", choices=["RXNORM", "ONCOTREE"])

    def handle(self, *args, **options):
        try:
            if options["translations"]:
                result = import_translations(options["path"], options["translations"])
            else:
                dataset = import_snapshot(options["path"])
                result = {
                    "id": str(dataset.pk),
                    "namespace": dataset.namespace,
                    "version": dataset.version,
                    "counts": dataset.counts,
                    "active": dataset.active,
                }
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
