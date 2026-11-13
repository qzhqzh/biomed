"""Evaluate reviewed local snapshots without touching operational data."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.curation.classification import DEFAULT_TAXONOMY, TAXONOMIES, taxonomy_snapshot
from apps.curation.evaluation import evaluate_samples, load_reviewed_samples


class Command(BaseCommand):
    help = "离线比较规则与已核读暂定标签；不下载、不改库、不训练"

    def add_arguments(self, parser):
        parser.add_argument("datasets", nargs="+")
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--taxonomy", choices=list(TAXONOMIES), action="append")
        parser.add_argument("--output", help="可选 JSON 输出文件；必须尚不存在")

    def handle(self, *args, **options):
        try:
            protocol, samples = load_reviewed_samples(
                options["datasets"], options["manifest"]
            )
            versions = options["taxonomy"] or [
                "solid-tumor-rules-v2.1",
                DEFAULT_TAXONOMY,
            ]
            result = evaluate_samples(
                protocol,
                samples,
                [taxonomy_snapshot(v) for v in dict.fromkeys(versions)],
            )
            serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if options["output"]:
                # Retain prior evaluations rather than silently replacing their evidence.
                with Path(options["output"]).open("x") as stream:
                    stream.write(serialized)
                self.stdout.write(
                    f"已写入 {options['output']}；暂定标签，不是人工金标准。"
                )
            else:
                self.stdout.write(serialized)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise CommandError(str(exc)) from exc
