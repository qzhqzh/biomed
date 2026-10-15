from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.curation.models import WorkerHeartbeat


class Command(BaseCommand):
    help = "Check the durable workflow worker heartbeat."

    def handle(self, *args, **options):
        beat = WorkerHeartbeat.objects.filter(name="curation").first()
        if not beat or (timezone.now() - beat.last_seen).total_seconds() > 45:
            raise CommandError("Worker heartbeat is stale")
