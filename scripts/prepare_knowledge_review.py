"""Freeze current candidates and same-version abstracts for a new independent review."""

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from apps.curation.knowledge_publication import build_review_packet
    from apps.curation.knowledge_storage import digest
    from apps.curation.models import KnowledgeCandidate, Passage

    rows = []
    candidates = KnowledgeCandidate.objects.filter(
        source_run="knowledge-intake-20261030"
    ).only("id", "source_run", "group_id", "version", "content_sha256",
           "import_sha256", "fact", "provenance").select_related("version__material")
    for candidate in candidates:
        extras = list(Passage.objects.filter(version_id=candidate.version_id,
                      locator__contains="abstract").values_list("id", flat=True))
        packet = build_review_packet(candidate, extra_passages=extras)
        rows.append({"input_sha256": digest(packet), "packet": packet})
    if len(rows) != 15:
        raise ValueError(f"Expected existing 15 groups, got {len(rows)}")
    output = {"schema_version": "knowledge-review-inputs-v1", "records": rows}
    data = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        if args.output.read_text() != data:
            raise ValueError("Refusing to overwrite different frozen review inputs")
    else:
        args.output.write_text(data)
    print(json.dumps({"groups": len(rows), "sha256": digest(output),
                      "path": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
