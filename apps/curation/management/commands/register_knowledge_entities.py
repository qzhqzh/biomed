"""Resume local entity normalization for already stored knowledge candidates."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.curation.entity_registry_integration import register_candidates
from apps.curation.knowledge_models import KnowledgeCandidate


class Command(BaseCommand):
    help = "Register real drug/disease participants and reuse local confirmed identities; no model or remote lookup."

    def add_arguments(self, parser):
        parser.add_argument("--source-run")
        parser.add_argument("--candidate-id", action="append", dest="candidate_ids")
        parser.add_argument("--chunk-size", type=int, default=50)

    def handle(self, *args, **options):
        size = options["chunk_size"]
        if not 1 <= size <= 200:
            raise CommandError("chunk-size must be 1–200")
        query = KnowledgeCandidate.objects.order_by("pk")
        if options["source_run"]:
            query = query.filter(source_run=options["source_run"])
        if options["candidate_ids"]:
            query = query.filter(pk__in=options["candidate_ids"])
        total, after = {}, None
        while True:
            page = query.filter(pk__gt=after) if after else query
            ids = list(page.values_list("pk", flat=True)[:size])
            if not ids:
                break
            try:
                result = register_candidates(ids, chunk_size=size)
            except (ValueError, TypeError) as exc:
                raise CommandError(str(exc)) from exc
            for key, value in result.items():
                if type(value) is int:
                    total[key] = total.get(key, 0) + value
            after = ids[-1]
        self.stdout.write(
            json.dumps({**total, "target_contacted": False, "model_called": False})
        )
