from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.curation.models import WorkerHeartbeat
from apps.curation.worker_heartbeat import worker_last_seen


class Command(BaseCommand):
    help = "Check the durable workflow worker heartbeat."

    def handle(self, *args, **options):
        seen = worker_last_seen(WorkerHeartbeat)
        if not seen or (timezone.now() - seen).total_seconds() > 45:
            raise CommandError("Worker heartbeat is stale")
