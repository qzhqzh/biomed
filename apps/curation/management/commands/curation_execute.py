from django.core.management.base import BaseCommand

from apps.curation.models import WorkItem
from apps.curation.workflows import execute


class Command(BaseCommand):
    help = "Internal worker subprocess. Requires an active lease token."

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument("token")

    def handle(self, *args, **options):
        item = WorkItem.objects.filter(
            pk=options["job_id"], state="running", lease_token=options["token"]
        ).first()
        if item:
            execute(item)
