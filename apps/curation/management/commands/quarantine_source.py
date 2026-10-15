from django.core.management.base import BaseCommand, CommandError

from apps.curation.materials import quarantine_version


class Command(BaseCommand):
    help = "Quarantine a source version, retain original bytes and block downstream evidence."

    def add_arguments(self, parser):
        parser.add_argument("--version", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        if not options["reason"].strip():
            raise CommandError("需要具体原因")
        version = quarantine_version(options["version"], options["reason"].strip())
        self.stdout.write(f"已隔离版本 {version.pk}；原文件与历史记录保留。")
