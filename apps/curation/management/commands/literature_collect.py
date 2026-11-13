import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from apps.curation.collections import (
    collection_data,
    control_collection,
    create_collection,
    start_full_collection,
)
from apps.curation.models import LiteratureCollection
from apps.curation.network import SourceBlocked


class Command(BaseCommand):
    help = "估算 / 控制有预算的实体瘤文献采集；实际步骤由 curation_worker 执行"

    def add_arguments(self, parser):
        parser.add_argument(
            "action", choices=["create", "status", "pause", "resume", "full"]
        )
        parser.add_argument("collection_id", nargs="?")
        parser.add_argument("--start")
        parser.add_argument("--end")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--fulltexts", type=int)
        parser.add_argument("--requests", type=int)
        parser.add_argument("--max-mib", type=int)
        parser.add_argument("--interval", type=int)
        parser.add_argument("--kib-per-second", type=int)
        parser.add_argument("--observation", help="full 使用的本地官方计数快照 JSON")

    def handle(self, *args, **options):
        limits = {}
        for key, dest, factor in [
            ("limit", "max_items", 1),
            ("fulltexts", "max_fulltexts", 1),
            ("requests", "max_requests", 1),
            ("max_mib", "max_bytes", 1024**2),
            ("interval", "interval_seconds", 1),
            ("kib_per_second", "bytes_per_second", 1024),
        ]:
            if options[key] is not None:
                limits[dest] = options[key] * factor
        try:
            if options["action"] == "create":
                if not options["start"] or not options["end"]:
                    raise CommandError(
                        "create 必须指定 --start 和 --end，日期格式 YYYY-MM-DD"
                    )
                run = create_collection(options["start"], options["end"], limits)
            elif options["action"] == "full":
                if not options["observation"]:
                    raise CommandError("full 必须提供 --observation 官方计数快照")
                path = Path(options["observation"])
                if path.stat().st_size > 1024 * 1024:
                    raise CommandError("计数快照不能超过 1 MiB")
                run = start_full_collection(
                    options["collection_id"], json.loads(path.read_text()), limits
                )
            elif options["action"] == "status":
                run = LiteratureCollection.objects.get(pk=options["collection_id"])
            else:
                run = control_collection(
                    options["collection_id"], options["action"], limits
                )
        except (
            SourceBlocked,
            ValueError,
            KeyError,
            TypeError,
            OSError,
            LiteratureCollection.DoesNotExist,
        ) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                collection_data(run, detail=True),
                cls=DjangoJSONEncoder,
                ensure_ascii=False,
                indent=2,
            )
        )
