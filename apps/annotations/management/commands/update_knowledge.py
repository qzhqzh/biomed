from django.core.management.base import BaseCommand, CommandError

from apps.annotations.knowledge.updater import UpdateBusy, start_update


class Command(BaseCommand):
    help = "Check NCBI Gene/MANE; publish a validated snapshot only when changed."

    def add_arguments(self, parser):
        parser.add_argument("--check-only", action="store_true")

    def handle(self, *args, **options):
        try:
            result = start_update("check" if options["check_only"] else "update", background=False)
        except (UpdateBusy, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if result["status"] != "succeeded":
            raise CommandError(result["message"])
        self.stdout.write(result["message"])
