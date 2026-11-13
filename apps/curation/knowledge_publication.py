"""Auditable model review of immutable, source-anchored knowledge candidates."""

import hashlib
import re
import unicodedata

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.curation.knowledge_storage import digest
from apps.curation.models import (
    KnowledgeCandidate,
    KnowledgeReview,
    KnowledgeSearchTerm,
    Passage,
)

ROLES = {"disease", "drug", "biomarker", "treatment", "gene", "variant", "protein"}
BINDING_FIELDS = {
    "population",
    "treatment",
    "drug_names",
    "comparator",
    "conditions",
    "results",
    "scope",
    "conclusion",
    "direction",
}


def normalize_term(term):
    return " ".join(unicodedata.normalize("NFKC", term).casefold().split())


def build_review_packet(candidate, extra_passages=()):
    """Freeze model inputs; deliberately exclude old assessments and mutable states.

    SHA256 is ``digest(packet)``: UTF-8 JSON, sorted keys, compact separators,
    ensure_ascii=False, allow_nan=False. Extra passages must belong to this version.
    This function reads only pre-publication candidate fields so packets can be
    exported with ``.only(...)`` before the additive publication migration.
    """
    version = candidate.version
    if version.integrity_status != "valid":
        raise ValueError("Source version is quarantined")
    anchors = list(candidate.anchors.select_related("entity", "passage").order_by("id"))
    passages = {str(anchor.passage_id): anchor.passage for anchor in anchors}
    extra_ids = sorted({str(getattr(item, "pk", item)) for item in extra_passages})
    extras = list(Passage.objects.filter(pk__in=extra_ids))
    if len(extras) != len(extra_ids):
        raise ValueError("Extra passage does not exist")
    for passage in extras:
        if passage.version_id != version.pk:
            raise ValueError("Extra passage belongs to a different source version")
        passages[str(passage.pk)] = passage
    entities = {}
    serialized_anchors = []
    for anchor in anchors:
        passage = anchor.passage
        if (
            passage.version_id != version.pk
            or passage.text[anchor.start : anchor.end] != anchor.quote
        ):
            raise ValueError("Original anchor no longer exactly matches source")
        if anchor.entity_id:
            entity = anchor.entity
            entities[str(entity.pk)] = {
                "id": str(entity.pk),
                "kind": entity.kind,
                "subtype": entity.subtype,
                "label": entity.label,
                "scope_version_id": str(entity.scope_version_id),
                "attributes": entity.attributes,
            }
        serialized_anchors.append(
            {
                "id": str(anchor.pk),
                "entity_id": str(anchor.entity_id) if anchor.entity_id else None,
                "entity_ref": anchor.entity_ref,
                "passage_id": str(passage.pk),
                "purpose": anchor.purpose,
                "association": anchor.association,
                "roles": anchor.roles,
                "start": anchor.start,
                "end": anchor.end,
                "quote": anchor.quote,
                "attributes": anchor.attributes,
            }
        )
    return {
        "schema_version": "knowledge-review-packet-v1",
        "candidate": {
            "id": str(candidate.pk),
            "source_run": candidate.source_run,
            "group_id": candidate.group_id,
            "content_sha256": candidate.content_sha256,
            "import_sha256": candidate.import_sha256,
            "fact": candidate.fact,
            "provenance": candidate.provenance,
        },
        "article": {
            "material_id": str(version.material_id),
            "version_id": str(version.pk),
            "title": version.material.title,
            "identifiers": version.material.identifiers,
            "sha256": version.sha256,
            "source_url": version.source_url,
            "rights": version.rights,
        },
        "entities": [entities[key] for key in sorted(entities)],
        "anchors": serialized_anchors,
        "passages": [
            {
                "id": key,
                "version_id": str(item.version_id),
                "parser_version": item.parser_version,
                "ordinal": item.ordinal,
                "locator": item.locator,
                "text": item.text,
                "text_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            }
            for key, item in sorted(passages.items())
        ],
        "extra_passage_ids": extra_ids,
    }


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def _quote(raw, passages):
    if not isinstance(raw, dict):
        raise TypeError("Quote must be an object")
    passage = passages.get(str(raw.get("passage_id")))
    start, end, quote = raw.get("start"), raw.get("end"), raw.get("quote")
    if (
        passage is None
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(passage["text"])
        or not isinstance(quote, str)
        or passage["text"][start:end] != quote
    ):
        raise ValueError("Review quote must exactly match a frozen source passage")
    result = {"passage_id": passage["id"], "start": start, "end": end, "quote": quote}
    if "fields" in raw:
        if not isinstance(raw["fields"], list) or not all(
            isinstance(f, str) for f in raw["fields"]
        ):
            raise ValueError("Quote fields must be a list of field paths")
        result["fields"] = raw["fields"]
    return result


def _validate_record(raw, packet, *, allow_undetermined=False):
    if raw.get("input_sha256") != digest(packet):
        raise ValueError(
            "Review input_sha256 mismatch; source or candidate inputs changed"
        )
    decision = raw.get("decision")
    decisions = {"pass", "fail", "undetermined"} if allow_undetermined else {"pass", "fail"}
    if decision not in decisions:
        raise ValueError("Invalid AI review decision for this review mode")
    reason = _text(raw.get("reason"), "reason", 20000)
    passages = {p["id"]: p for p in packet["passages"]}
    entities = {e["id"]: e for e in packet["entities"]}
    refs = {
        a["entity_ref"]: a["entity_id"] for a in packet["anchors"] if a["entity_id"]
    }
    if not isinstance(raw.get("approved_links", []), list) or not isinstance(
        raw.get("supporting_quotes", []), list
    ):
        raise TypeError("approved_links and supporting_quotes must be lists")
    links = []
    seen = set()
    for link in raw.get("approved_links", []):
        if not isinstance(link, dict):
            raise TypeError("Approved link must be an object")
        if not isinstance(link.get("aliases", []), list) or not isinstance(
            link.get("support", []), list
        ):
            raise TypeError("Link aliases and support must be lists")
        entity_id = str(link.get("entity_id") or refs.get(link.get("entity_ref"), ""))
        entity = entities.get(entity_id)
        role = link.get("role")
        if entity is None or role not in ROLES:
            raise ValueError(
                "Approved link must reference a packet entity and supported role"
            )
        if link.get("entity_ref") and refs.get(link["entity_ref"]) != entity_id:
            raise ValueError("entity_ref and entity_id disagree")
        if role != "biomarker" and role != entity["kind"]:
            raise ValueError("Approved role contradicts the source entity type")
        if (entity_id, role) in seen:
            raise ValueError("Duplicate approved entity and role")
        seen.add((entity_id, role))
        support = [_quote(q, passages) for q in link.get("support", [])]
        if not support:
            raise ValueError("Every approved entity needs exact source support")
        aliases = []
        normalized = set()
        for alias in [
            {"term": entity["label"], "basis": "source_label"},
            *link.get("aliases", []),
        ]:
            if not isinstance(alias, dict):
                raise TypeError("Alias must be an object")
            term = _text(alias.get("term"), "alias term")
            basis = _text(alias.get("basis"), "alias basis")
            norm = normalize_term(term)
            if norm not in normalized:
                normalized.add(norm)
                aliases.append({"term": term, "basis": basis})
        links.append(
            {
                "entity_id": entity_id,
                "entity_ref": link.get("entity_ref", ""),
                "role": role,
                "label": entity["label"],
                "aliases": aliases,
                "support": support,
                **(
                    {"context": _text(link["context"], "link context")}
                    if "context" in link
                    else {}
                ),
            }
        )
    quotes = [_quote(q, passages) for q in raw.get("supporting_quotes", [])]
    if decision == "pass":
        if not links or not quotes:
            raise ValueError(
                "Passing review requires approved relationship links and source quotes"
            )
        required = {
            key for key in BINDING_FIELDS if packet["candidate"]["fact"].get(key)
        }
        covered = {
            re.split(r"[.\[]", field, maxsplit=1)[0]
            for q in quotes
            for field in q.get("fields", [])
        }
        if missing := required - covered:
            raise ValueError(
                "Passing review lacks source bindings for: "
                + ", ".join(sorted(missing))
            )
        # Some old drug_names include a regimen/category (e.g. cCRT). Keep that
        # fact intact and require an exactly named, correctly typed link rather
        # than manufacturing a drug. Specific drugs still use existing drug entities.
        approved_drugs = {
            normalize_term(a["term"])
            for link in links
            if link["role"] in {"drug", "treatment"}
            for a in link["aliases"]
        }
        specific_drugs = {
            normalize_term(e["label"]) for e in entities.values() if e["kind"] == "drug"
        }
        # Old extraction can include both a mistaken drug and a correct category
        # for the same source text (e.g. ICTs). Independent review can choose the
        # existing category; an arbitrary treatment alias cannot override a drug.
        reviewed_categories = {
            normalize_term(link["label"])
            for link in links
            if link["role"] == "treatment"
        }
        specific_drugs -= reviewed_categories
        approved_specific = {
            normalize_term(a["term"])
            for link in links
            if link["role"] == "drug"
            for a in link["aliases"]
        }
        drugs = packet["candidate"]["fact"].get("drug_names", [])
        if not isinstance(drugs, list) or any(
            normalize_term(drug) not in approved_drugs for drug in drugs
        ):
            raise ValueError("Every explicit drug_name needs an approved drug link")
        if any(
            normalize_term(drug) in specific_drugs
            and normalize_term(drug) not in approved_specific
            for drug in drugs
        ):
            raise ValueError(
                "A specific drug cannot be replaced by a treatment category"
            )
    return {
        "candidate_id": packet["candidate"]["id"],
        "input_sha256": digest(packet),
        "extra_passage_ids": packet["extra_passage_ids"],
        "decision": decision,
        "reason": reason,
        "approved_links": links,
        "supporting_quotes": quotes,
    }


def _validate_target_quality(raw, packet):
    """Record target-field quality without imposing publication completeness.

    The evaluator supplies the judgement and three separate dimensions. This
    validator checks their provenance and shape, not every historical fact slot.
    """
    if raw.get("input_sha256") != digest(packet):
        raise ValueError("Review input_sha256 mismatch; source or candidate inputs changed")
    decision = raw.get("decision")
    if decision not in {"pass", "fail", "undetermined"}:
        raise ValueError("Invalid target quality decision")
    reason = _text(raw.get("reason"), "reason", 20000)
    dimensions = raw.get("quality_dimensions")
    keys = {"core_valid", "target_covered", "participants_usable"}
    if (
        not isinstance(dimensions, dict)
        or set(dimensions) != keys
        or any(type(value) is not bool for value in dimensions.values())
    ):
        raise ValueError("quality_dimensions requires core_valid, target_covered and participants_usable booleans")
    issues = raw.get("issues", [])
    if not isinstance(issues, list) or any(
        not isinstance(issue, (str, dict)) for issue in issues
    ):
        raise ValueError("issues must be an array of text or diagnostic objects")
    digest(issues)  # Only finite JSON data may enter an immutable review event.
    raw_quotes = raw.get("supporting_quotes", [])
    if not isinstance(raw_quotes, list):
        raise TypeError("supporting_quotes must be a list")
    passages = {passage["id"]: passage for passage in packet["passages"]}
    quotes = [_quote(quote, passages) for quote in raw_quotes]
    if packet["candidate"]["fact"] and not quotes:
        raise ValueError("Target quality of an extracted fact requires at least one exact source quote")
    return {
        "schema_version": "knowledge-quality-v1",
        "candidate_id": packet["candidate"]["id"],
        "input_sha256": digest(packet),
        "extra_passage_ids": packet["extra_passage_ids"],
        "decision": decision,
        "reason": reason,
        "quality_dimensions": dimensions,
        "issues": issues,
        "supporting_quotes": quotes,
    }


def apply_review_batch(payload, *, dry_run=False, publish=True):
    """Import actual, independently produced reviews; never invoke or simulate AI.

    Validate the whole file first, then recheck inputs in one bounded transaction.
    New review runs append history. Replaying a review is idempotent, and neither
    replay nor a new AI pass can resurrect a withdrawn candidate. With
    ``publish=False``, append independent quality events only; extraction,
    publication, current publication review and search terms are unchanged.
    """
    if not isinstance(payload, dict):
        raise TypeError("Review batch must be an object")
    target_quality = payload.get("schema_version") == "knowledge-quality-v1"
    if target_quality and publish:
        raise ValueError("knowledge-quality-v1 is only supported in quality-only mode")
    meta = {
        key: _text(payload.get(key), key, 100)
        for key in ("review_run", "provider", "model", "policy_version")
    }
    records = payload.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise ValueError("A review batch must contain 1–100 records")
    prepared = []
    ids = set()
    for raw in records:
        if not isinstance(raw, dict):
            raise TypeError("Review record must be an object")
        candidate = KnowledgeCandidate.objects.select_related("version__material").get(
            pk=raw.get("candidate_id")
        )
        if candidate.pk in ids:
            raise ValueError("Duplicate candidate in review batch")
        ids.add(candidate.pk)
        extras = raw.get("extra_passage_ids", [])
        if not isinstance(extras, list):
            raise TypeError("extra_passage_ids must be a list")
        packet = build_review_packet(candidate, extras)
        result = (
            _validate_target_quality(raw, packet)
            if target_quality
            else _validate_record(raw, packet, allow_undetermined=not publish)
        )
        key = digest(["ai" if publish else "quality", meta["review_run"], str(candidate.pk)])
        previous = KnowledgeReview.objects.filter(review_key=key).first()
        if previous and (previous.result != result or previous.provenance != meta):
            raise ValueError("Review run already exists with different content")
        prepared.append((candidate.pk, packet, result, key))
    summary = {
        "review_run": meta["review_run"],
        "dry_run": dry_run,
        "mode": "publication" if publish else "quality_only",
        "review_count": len(prepared),
        "created": 0,
        "reused": 0,
        "published": 0,
        "results": [],
    }
    if dry_run:
        summary["decisions"] = {
            decision: sum(r[2]["decision"] == decision for r in prepared)
            for decision in (("pass", "fail") if publish else ("pass", "fail", "undetermined"))
        }
        return summary
    with transaction.atomic():
        for pk, packet, result, key in prepared:
            candidate = (
                KnowledgeCandidate.objects.select_for_update()
                .select_related("version__material")
                .get(pk=pk)
            )
            current_packet = build_review_packet(candidate, result["extra_passage_ids"])
            if digest(current_packet) != result["input_sha256"]:
                raise ValueError("Review inputs changed before publication")
            previous = KnowledgeReview.objects.filter(review_key=key).first()
            if previous:
                if previous.result != result or previous.provenance != meta:
                    raise ValueError("Conflicting review replay")
                summary["reused"] += 1
            else:
                review = KnowledgeReview.objects.create(
                    candidate=candidate,
                    review_key=key,
                    kind="ai" if publish else "quality",
                    decision=result["decision"],
                    reason=result["reason"],
                    input_sha256=result["input_sha256"],
                    input_snapshot=packet,
                    result=result,
                    provenance=meta,
                    **meta,
                )
                terms = []
                if publish and result["decision"] == "pass":
                    for link in result["approved_links"]:
                        for alias in link["aliases"]:
                            terms.append(
                                KnowledgeSearchTerm(
                                    candidate=candidate,
                                    review=review,
                                    entity_id=link["entity_id"],
                                    role=link["role"],
                                    term=alias["term"],
                                    basis=alias["basis"],
                                    normalized_term=normalize_term(alias["term"]),
                                )
                            )
                KnowledgeSearchTerm.objects.bulk_create(terms)
                if publish:
                    candidate.current_review = review
                    candidate.ai_status = result["decision"]
                    if (
                        result["decision"] == "fail"
                        and candidate.publication_status == "published"
                    ):
                        candidate.publication_status = "withdrawn"
                    elif (
                        result["decision"] == "pass"
                        and candidate.publication_status == "draft"
                        and candidate.human_status != "flagged"
                    ):
                        candidate.publication_status = "published"
                        candidate.published_at = timezone.now()
                    candidate.save(
                        update_fields=[
                            "current_review",
                            "ai_status",
                            "publication_status",
                            "published_at",
                        ]
                    )
                summary["created"] += 1
            summary["published"] += candidate.publication_status == "published"
            summary["results"].append(
                {
                    "candidate_id": str(pk),
                    "group_id": candidate.group_id,
                    "ai_status": candidate.ai_status,
                    "publication_status": candidate.publication_status,
                }
            )
    return summary


def human_review(candidate_id, user, decision, reason):
    if not user.is_authenticated or not user.has_perm("curation.change_evidence"):
        raise PermissionDenied("需要登录并具备证据抽查权限")
    if decision not in {"confirm", "flag"}:
        raise ValueError("人工抽查只能 confirm 或 flag")
    reason = _text(reason, "reason", 10000)
    with transaction.atomic():
        candidate = (
            KnowledgeCandidate.objects.select_for_update()
            .select_related("version")
            .get(pk=candidate_id)
        )
        key = digest(
            [
                "human",
                str(candidate.pk),
                str(candidate.current_review_id),
                str(user.pk),
                decision,
                reason,
            ]
        )
        if KnowledgeReview.objects.filter(review_key=key).exists():
            return candidate
        if (
            candidate.ai_status != "pass"
            or candidate.publication_status != "published"
            or candidate.version.integrity_status != "valid"
        ):
            raise ValueError(
                "只能抽查已通过 AI 核验且仍在发布的知识；撤回后不能覆盖历史重新发布"
            )
        KnowledgeReview.objects.create(
            candidate=candidate,
            review_key=key,
            kind="human",
            decision=decision,
            reason=reason,
            actor=user,
            policy_version="knowledge-human-audit-v1",
            review_run="human-audit",
            input_sha256=candidate.current_review.input_sha256,
            input_snapshot={"ai_review_id": str(candidate.current_review_id)},
            result={"decision": decision, "reason": reason},
            provenance={"ai_review_id": str(candidate.current_review_id)},
        )
        candidate.human_status = "confirmed" if decision == "confirm" else "flagged"
        if decision == "flag":
            candidate.publication_status = "withdrawn"
        candidate.save(update_fields=["human_status", "publication_status"])
    return candidate
