"""Append validated local model candidates; retain every previous classification."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.curation.classification import (
    classify_metadata,
    taxonomy_hash,
    taxonomy_snapshot,
)
from apps.curation.decision import (
    FIELDS,
    SCOPE_VERSION,
    VERSION,
    checked_input,
    digest,
    validate_decision,
)
from apps.curation.materials import pubmed_metadata, pubmed_record_id, pubmed_records
from apps.curation.models import LiteratureClassification, MaterialVersion
from apps.curation.network import SourceBlocked, xml
from apps.curation.storage import read_blob


def import_decisions(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    config = manifest["config"]
    if (
        config["version"] not in (VERSION, SCOPE_VERSION)
        or digest(config) != manifest["config_sha256"]
    ):
        raise ValueError("模型运行配置校验失败")
    rows = [
        checked_input(r)
        for r in json.loads((directory / "inputs.json").read_text())["records"]
    ]
    if (
        digest(rows) != manifest["input_sha256"]
        or len(rows) != manifest["records"]
        or len({r["pmid"] for r in rows}) != len(rows)
    ):
        raise ValueError("模型运行输入集合校验失败")
    rule_taxonomy = taxonomy_snapshot("solid-tumor-rules-v3")
    pending = []
    rejected = 0
    for row in rows:
        prediction = json.loads((directory / f"result-{row['pmid']}.json").read_text())
        if (
            any(
                prediction[k] != row[k]
                for k in ("pmid", "version_id", "source_sha256", "input_sha256")
            )
            or prediction["config_sha256"] != manifest["config_sha256"]
        ):
            raise ValueError("模型输出与来源版本不一致")
        if prediction["status"] not in {
            "validated_candidate",
            "invalid_output",
            "input_too_long",
            "output_truncated",
        }:
            raise ValueError("未知模型输出状态")
        if prediction["status"] != "validated_candidate":
            rejected += 1
            continue
        result = validate_decision(prediction["raw_output"], row["metadata"])
        if result != prediction["decision"]:
            raise ValueError("模型输出规范化结果不一致")
        version = MaterialVersion.objects.select_related("material").get(
            pk=row["version_id"]
        )
        if (
            version.sha256 != row["source_sha256"]
            or version.integrity_status != "valid"
            or version.material.source_key != "pubmed:" + row["pmid"]
        ):
            raise ValueError("不可变来源身份校验失败")
        records = pubmed_records(xml(read_blob(version.sha256)))
        if len(records) != 1 or pubmed_record_id(records[0]) != row["pmid"]:
            raise ValueError("原始元数据 PMID 不一致")
        metadata = pubmed_metadata(records[0])
        if {k: metadata[k] for k in FIELDS} != row["metadata"]:
            raise ValueError("模型输入不等于已存原始元数据")
        source_status = classify_metadata(metadata, rule_taxonomy)
        result.update(
            {
                "deployment_status": "experimental",
                "input": source_status["input"],
                "publication_status": source_status["publication_status"],
                "publication_status_basis": source_status["publication_status_basis"],
                "publication_status_conflicts": source_status[
                    "publication_status_conflicts"
                ],
                "publication_status_taxonomy_sha256": taxonomy_hash(rule_taxonomy),
                "model_provenance": {
                    "config_sha256": manifest["config_sha256"],
                    "config": config,
                    "input_sha256": row["input_sha256"],
                    "source_sha256": row["source_sha256"],
                    "created_at": prediction["created_at"],
                    "seconds": prediction["seconds"],
                    "input_tokens": prediction["input_tokens"],
                    "output_tokens": prediction["output_tokens"],
                },
                "raw_output": prediction["raw_output"],
            }
        )
        pending.append((version, result))
    # GPU inference and source validation never hold a writer transaction.
    created = reused = 0
    with transaction.atomic():
        for version, result in pending:
            if not MaterialVersion.objects.filter(
                pk=version.pk, integrity_status="valid"
            ).exists():
                raise ValueError("来源完整性状态已变化")
            _, new = LiteratureClassification.objects.get_or_create(
                version=version,
                taxonomy_sha256=manifest["config_sha256"],
                defaults={"taxonomy_version": config["version"], "result": result},
            )
            created += int(new)
            reused += int(not new)
    return {
        "processed": len(rows),
        "created": created,
        "reused": reused,
        "rejected_outputs": rejected,
        "taxonomy_version": config["version"],
        "review_status": "unreviewed",
        "evidence_published": 0,
    }


class Command(BaseCommand):
    help = "核对原文后追加本地模型候选分类；不替换规则、不发布证据"

    def add_arguments(self, parser):
        parser.add_argument("directory")

    def handle(self, *args, **options):
        try:
            result = import_decisions(options["directory"])
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OSError,
            SourceBlocked,
            MaterialVersion.DoesNotExist,
        ) as exc:
            raise CommandError(str(exc)) from exc
