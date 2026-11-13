"""Seed two frozen source-linked candidates into a new local test database only."""

import hashlib
import json
import os
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings

from apps.curation.knowledge_storage import import_candidates
from apps.curation.models import KnowledgeCandidate, Material, MaterialVersion, Passage
from scripts.knowledge_observation_report import digest

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "var/experiments/target-identity-scope-20261112-v1"
DATABASE = Path("/tmp/biomed-scope-display-isolated-20261112.sqlite3")
RUN = "scope-display-isolation-20261112"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    configured = Path(settings.DATABASES["default"]["NAME"])
    if (os.environ.get("BIOMED_SCOPE_ISOLATED") != "1" or not settings.DEBUG or configured != DATABASE
            or configured.is_symlink() or configured.resolve() != DATABASE
            or Material.objects.exists() or KnowledgeCandidate.objects.exists()):
        raise ValueError("Seed is allowed only in the explicitly selected empty /tmp test database")
    manifest, source = read(ROOT / "manifest.json"), read(ROOT / "source-only.json")
    preview_path = ROOT / "target-preview-astra-reviewed-v1.json"
    preview = read(preview_path)
    runtime_manifest = read(PROJECT / "var/experiments/target-runtime-isolation-20261112-v1/manifest.json")
    if (digest(source) != manifest["source_only_sha256"] or sha(preview_path) != runtime_manifest["preview_sha256"]
            or preview["record_count"] != 2 or preview["submit_ready"] or preview["target_written"]):
        raise ValueError("Frozen source or reviewed candidate differs")
    articles = {article["pmid"]: article for article in source["articles"]}
    records = []
    for item in preview["records"]:
        record, article = item["biomed_companion"], articles[item["pmid"]]
        if (item["semantic_review_status"] != "astra_source_supported" or record["source_sha256"] != article["source_sha256"]
                or str(record["version_id"]).replace("-", "") != str(article["version_id"]).replace("-", "")):
            raise ValueError("Candidate and source do not match")
        original = record["article"]
        material = Material.objects.create(id=original["material_id"], source_key=f"isolated-scope:{item['pmid']}",
                    kind="paper", title=original["title"], identifiers={"pubmed": item["pmid"],
                    "pmc": original.get("pmcid") or "", "doi": original.get("doi") or ""},
                    metadata={"isolation_only": True, "source_reference": "frozen Astra-reviewed preview"})
        version = MaterialVersion.objects.create(id=record["version_id"], material=material, sha256=record["source_sha256"],
                    media_type="application/xml", filename="isolated-source.xml", source_url=original.get("source_url") or "",
                    rights=original.get("rights") or "", integrity_status="valid")
        used = {anchor["passage_id"] for anchor in record["anchors"]}
        rows = [row for row in article["passages"] if row["id"] in used]
        if {row["id"] for row in rows} != used:
            raise ValueError("Selected source passage is missing")
        for ordinal, row in enumerate(rows, 1):
            parser_versions = {anchor.get("parser_version") for anchor in record["anchors"]
                               if anchor["passage_id"] == row["id"] and anchor.get("parser_version")}
            if len(parser_versions) > 1:
                raise ValueError("Source passage has conflicting parser versions")
            Passage.objects.create(id=row["id"], version=version, parser_version=next(iter(parser_versions), "isolation-source"),
                                   ordinal=ordinal, locator=row["locator"], text=row["text"])
        records.append(record)
    receipt = import_candidates({"schema_version": "knowledge-candidates-v1", "source_run": RUN,
                                 "provenance": {"isolation_only": True, "source_preview_sha256": sha(preview_path)},
                                 "records": records})
    candidates = list(KnowledgeCandidate.objects.filter(source_run=RUN).order_by("group_id"))
    if len(candidates) != 2 or any(candidate.publication_status != "draft" or candidate.ai_status != "pending"
                                   for candidate in candidates):
        raise ValueError("Expected two unmodified, unpublished isolation candidates")
    return {"database": str(DATABASE), "source_run": RUN, "receipt": receipt,
            "candidates": [{"id": str(item.pk), "group_id": item.group_id} for item in candidates],
            "production_written": False, "target_written": False, "ai_publication_created": False}


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False))
