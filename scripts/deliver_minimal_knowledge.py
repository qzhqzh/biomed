"""Deliver the selected frozen knowledge bundle, then append independent quality.

No models, target writes, migrations, publication decisions or historical rewrites.
Use --apply explicitly; the default validates sources and prepares local exports.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.knowledge_export import export_bundle

SOURCE_RUN = "minimal-knowledge-selected-20261101"
DIMENSIONS = ("core_valid", "target_covered", "participants_usable")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path, value):
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise ValueError(f"Refusing to overwrite frozen artifact: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(data)


def prepare_delivery(source, source_sha256, *, source_run=SOURCE_RUN):
    bundle = copy.deepcopy(source)
    bundle["source_run"] = source_run
    bundle.setdefault("provenance", {})["selected_source"] = {
        "source_run": source["source_run"],
        "bundle_sha256": source_sha256,
        "delivery_version": "minimal-knowledge-delivery-v1",
        "semantic_review_is_independent": True,
    }
    preview = export_bundle(bundle)
    compatibility = {row["group_id"]: row for row in preview["records"]}
    for record in bundle["records"]:
        exported = compatibility[record["group_id"]]
        assessment = record.setdefault("assessment", {})
        assessment["quality"] = {"status": "unreviewed"}
        assessment["compatibility"] = {
            "status": exported["compatibility_status"],
            "issues": exported["mapping_issues"],
            "contract_validation": exported["contract_validation"],
            "offline_ready": exported["offline_ready"],
            "submit_ready": False,
            "contract_version": preview["contract"]["version"],
        }
    # Re-export after attaching statuses, so the companion and bundle hash match
    # the actual delivered object; no quality verdict affects compatibility.
    return bundle, export_bundle(bundle)


def selected_reviews(source, bundle_sha256, final, prior, prior_sha256):
    if final.get("bundle_sha256", {}).get("A") != bundle_sha256:
        raise ValueError("Final Astra review does not bind this source bundle SHA256")
    if final.get("prior_review_sha256", {}).get("A") != prior_sha256:
        raise ValueError("Prior Astra semantic review SHA256 does not match")
    expected = {row["group_id"] for row in source["records"]}
    final_rows = {row["id"]: row["arms"]["A"] for row in final["records"]}
    prior_rows = {row["id"]: row for row in prior["records"]}
    if (
        len(final_rows) != len(final["records"])
        or len(prior_rows) != len(prior["records"])
        or set(final_rows) != expected
        or set(prior_rows) != expected
    ):
        raise ValueError("Review groups must exactly match the selected bundle")
    for group, row in final_rows.items():
        if any(type(row.get(key)) is not bool for key in DIMENSIONS):
            raise ValueError(f"Explicit quality booleans missing: {group}")
        if any(row[key] != prior_rows[group][key] for key in ("core_valid", "target_covered")):
            raise ValueError("Final assembly review changed the frozen semantic judgement")
    return final_rows, prior_rows


def exact_quote(raw, passages):
    passage_id = str(uuid.UUID(str(raw.get("passage_id") or raw.get("passage"))))
    passage = passages.get(passage_id)
    quote = raw.get("quote")
    if passage is None or not isinstance(quote, str) or not quote:
        raise ValueError("Astra quote has an unknown passage or empty text")
    text = passage["text"]
    if "start" in raw or "end" in raw:
        start, end = raw.get("start"), raw.get("end")
    else:
        start = text.find(quote)
        end = start + len(quote)
        if start >= 0 and text.find(quote, start + 1) >= 0:
            raise ValueError("Ambiguous repeated quote requires an explicit source offset")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != quote:
        raise ValueError("Astra quote does not exactly match its source")
    return {"passage_id": passage_id, "start": start, "end": end, "quote": quote}


def raw_quotes(value):
    """Retain actual reviewer evidence, including diagnostic source quotations."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_quotes":
                if not isinstance(item, list):
                    raise ValueError("source_quotes must be an array")
                yield from item
            else:
                yield from raw_quotes(item)
    elif isinstance(value, list):
        for item in value:
            yield from raw_quotes(item)


def quality_record(candidate, final_row, prior_row, *, build_packet):
    from apps.curation.knowledge_storage import digest

    quotes = list(raw_quotes(final_row)) + list(prior_row.get("source_quotes", []))
    extra_ids = sorted({str(uuid.UUID(str(q.get("passage_id") or q.get("passage")))) for q in quotes})
    packet = build_packet(candidate, extra_ids)
    passages = {row["id"]: row for row in packet["passages"]}
    exact = {}
    for quote in quotes:
        matched = exact_quote(quote, passages)
        exact[digest(matched)] = matched
    if candidate.fact and not exact:
        raise ValueError(f"Missing reviewer source quotes: {candidate.group_id}")
    dimensions = {key: final_row[key] for key in DIMENSIONS}
    issues = [
        {"dimension": key, "issue": item}
        for key in ("core_errors", "missing_core", "participant_errors", "missing_participants", "diagnostics")
        for item in final_row.get(key, [])
    ]
    reason = "\n".join(
        value for value in (prior_row.get("rationale"), final_row.get("review_basis"))
        if isinstance(value, str) and value.strip()
    )
    if not reason:
        raise ValueError("Actual reviewer rationale is required")
    return {
        "candidate_id": str(candidate.pk),
        "input_sha256": digest(packet),
        "extra_passage_ids": packet["extra_passage_ids"],
        "decision": "pass" if dimensions["core_valid"] and dimensions["participants_usable"] else "fail",
        "reason": reason,
        "supporting_quotes": list(exact.values()),
        "quality_dimensions": dimensions,
        "issues": issues,
    }


def old_snapshot(candidate_ids):
    from apps.curation.models import KnowledgeAnchor, KnowledgeCandidate

    rows = list(KnowledgeCandidate.objects.filter(pk__in=candidate_ids).order_by("id").values(
        "id", "source_run", "group_id", "content_sha256", "import_sha256", "fact", "assessment",
        "provenance", "status", "ai_status", "human_status", "publication_status", "current_review_id",
    ))
    for row in rows:
        row["id"] = str(row["id"])
        if row["current_review_id"]:
            row["current_review_id"] = str(row["current_review_id"])
    anchors = list(KnowledgeAnchor.objects.filter(candidate_id__in=candidate_ids).order_by("id").values_list("content_sha256", "quote"))
    return {"candidates": rows, "anchors": [list(row) for row in anchors]}


def execute(args):
    source = read(args.bundle)
    source_sha = file_sha(args.bundle)
    bundle, exported = prepare_delivery(source, source_sha, source_run=args.source_run)
    final, prior = read(args.review), read(args.prior_review)
    selected, prior_rows = selected_reviews(source, source_sha, final, prior, file_sha(args.prior_review))
    root = args.output
    freeze(root / "bundle.json", bundle)
    freeze(root / "tumor-api-export.json", exported)
    freeze(root / "review-binding.json", {
        "source_bundle_sha256": source_sha,
        "final_review_sha256": file_sha(args.review),
        "prior_review_sha256": file_sha(args.prior_review),
        "model": final["model"],
        "scope": final.get("scope"),
        "policy_clarification": final.get("policy_clarification"),
    })
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.conf import settings

    from apps.curation.knowledge_publication import (
        apply_review_batch,
        build_review_packet,
    )
    from apps.curation.knowledge_storage import digest, import_candidates
    from apps.curation.models import KnowledgeCandidate

    actual_db = Path(settings.DATABASES["default"]["NAME"]).resolve()
    if actual_db != args.expected_database.resolve() or not actual_db.is_file():
        raise ValueError("Expected existing database does not match Django configuration")
    dry_run = import_candidates(bundle, dry_run=True)
    attempt = root / ("apply-" if args.apply else "dry-run-") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    freeze(attempt / "intake-dry-run.json", dry_run)
    if not args.apply:
        return {"applied": False, "source_run": args.source_run, "candidates": dry_run["candidate_count"],
                "compatibility": exported["counts"], "offline_ready": exported["offline_ready_count"]}
    old_ids = list(KnowledgeCandidate.objects.exclude(source_run=args.source_run).values_list("id", flat=True))
    before = old_snapshot(old_ids)
    freeze(attempt / "historical-before.json", before)
    imported = import_candidates(bundle)
    replayed = import_candidates(bundle)
    if not replayed["reused"] or imported["candidates"] != replayed["candidates"]:
        raise ValueError("Candidate replay was not idempotent")
    candidates = list(KnowledgeCandidate.objects.filter(source_run=args.source_run).select_related("version__material"))
    if len(candidates) != len(bundle["records"]):
        raise ValueError("Delivered candidate count differs")
    states_before = {str(c.pk): [c.ai_status, c.publication_status, str(c.current_review_id)] for c in candidates}
    quality = {
        "schema_version": "knowledge-quality-v1",
        "review_run": "minimal-selected-20261101-" + file_sha(args.review),
        "provider": "openai", "model": final["model"], "policy_version": "minimal-target-core-quality-v1",
        "records": [quality_record(c, selected[c.group_id], prior_rows[c.group_id], build_packet=build_review_packet) for c in candidates],
    }
    freeze(root / "quality-input.json", quality)
    freeze(attempt / "quality-dry-run.json", apply_review_batch(quality, publish=False, dry_run=True))
    reviewed = apply_review_batch(quality, publish=False)
    quality_replay = apply_review_batch(quality, publish=False)
    if quality_replay["reused"] != len(candidates) or quality_replay["created"]:
        raise ValueError("Quality replay was not idempotent")
    for candidate in candidates:
        candidate.refresh_from_db()
        if states_before[str(candidate.pk)] != [candidate.ai_status, candidate.publication_status, str(candidate.current_review_id)]:
            raise ValueError("Independent quality changed publication state")
    after = old_snapshot(old_ids)
    if after != before:
        raise ValueError("Historical knowledge was changed")
    freeze(attempt / "historical-after.json", after)
    by_group = {c.group_id: c for c in candidates}
    for row in bundle["records"]:
        candidate = by_group[row["group_id"]]
        if candidate.fact != row["fact"] or candidate.assessment != row["assessment"]:
            raise ValueError("Delivered extraction data changed during quality review")
        build_review_packet(candidate)  # Recheck source versions and all exact anchors.
    receipt = {
        "applied": True, "source_run": args.source_run,
        "candidate_count": len(candidates), "historical_candidates_preserved": len(old_ids),
        "total_candidates": KnowledgeCandidate.objects.count(),
        "intake": imported, "intake_replay": replayed,
        "quality": reviewed, "quality_replay": quality_replay,
        "quality_dimensions": {key: sum(row[key] for row in selected.values()) for key in DIMENSIONS},
        "compatibility": exported["counts"], "offline_ready_count": exported["offline_ready_count"],
        "submit_ready": exported["submit_ready"],
        "bundle_sha256": file_sha(root / "bundle.json"),
        "quality_input_sha256": digest(quality),
        "publication_states_unchanged": True, "historical_data_unchanged": True,
        "source_spans_rechecked": True,
    }
    freeze(attempt / "receipt.json", receipt)
    (root / "latest-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: receipt[key] for key in ("applied", "source_run", "candidate_count", "historical_candidates_preserved", "total_candidates", "quality_dimensions", "compatibility", "offline_ready_count", "submit_ready")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--prior-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-database", type=Path, required=True)
    parser.add_argument("--source-run", default=SOURCE_RUN)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(execute(parser.parse_args()), ensure_ascii=False))


if __name__ == "__main__":
    main()
