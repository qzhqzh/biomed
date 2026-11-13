import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.annotations.knowledge.transcript_structures import build_release


class Command(BaseCommand):
    help = "Publish versioned transcript exon structures without touching the application database."
    requires_system_checks = ()

    def add_arguments(self, parser):
        parser.add_argument("--hg19", type=Path)
        parser.add_argument("--hg38", type=Path)
        parser.add_argument("--mane", type=Path)
        parser.add_argument("--genbank-dir", type=Path)
        parser.add_argument("--root", type=Path)
        parser.add_argument("--source-metadata", type=Path, help="JSON source URLs, releases and observed_at; unknown values stay null")

    def handle(self, *args, **options):
        try:
            metadata_path = options["source_metadata"]
            if metadata_path is None and options["genbank_dir"] and options["genbank_dir"].is_dir():
                candidate = options["genbank_dir"] / "sources.json"
                metadata_path = candidate if candidate.is_file() else None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path else {}
            result = build_release(
                hg19=options["hg19"], hg38=options["hg38"], mane=options["mane"],
                genbank_dir=options["genbank_dir"], root=options["root"], source_metadata=metadata,
            )
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
