"""Validate and append immutable research candidates without invoking a model."""

import hashlib
import json
import re
import uuid

from django.db import transaction

from apps.curation.models import (
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    MaterialVersion,
    Passage,
)

SCHEMA = "knowledge-candidates-v1"
KINDS = {"disease", "drug", "gene", "protein", "variant", "biomarker", "treatment"}
PURPOSES = {"mention", "evidence", "definition", "context"}
ASSOCIATIONS = {"model_selected", "exact_fact_text", "context_only", "definition"}
NAMESPACE = uuid.UUID("ef6a25d6-5b64-4a92-89de-f86817904145")


def digest(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _object(value, label):
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    digest(value)
    return value


def _text(value, label, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise ValueError(f"Invalid {label}")
    return value


def _uuid(value, label):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"Invalid {label}") from error


def _list(value, label, maximum, *, empty=True):
    if not isinstance(value, list) or len(value) > maximum or (not empty and not value):
        raise ValueError(f"Invalid {label}")
    return value


def _validate_article(record, version):
    if version.integrity_status != "valid":
        raise ValueError("Source version is not valid")
    if record.get("source_sha256") != version.sha256:
        raise ValueError("Source SHA256 does not match the stored version")
    article = _object(record.get("article", {}), "article")
    if (
        article.get("material_id")
        and _uuid(article["material_id"], "material_id") != version.material_id
    ):
        raise ValueError("Article identity does not match source version")
    identifiers = version.material.identifiers
    if (
        identifiers.get("pubmed")
        and identifiers.get("pmid")
        and str(identifiers["pubmed"]) != str(identifiers["pmid"])
    ):
        raise ValueError("Stored pubmed and pmid identifiers conflict")
    for source_key, target_keys in (
        ("pmid", ("pubmed", "pmid")),
        ("pmcid", ("pmc", "pmcid")),
        ("doi", ("doi",)),
    ):
        supplied = article.get(source_key)
        stored = next(
            (identifiers.get(key) for key in target_keys if identifiers.get(key)), None
        )
        if supplied and str(supplied) != str(stored):
            raise ValueError(f"Article {source_key} does not match stored identity")


def validate_payload(payload):
    """Resolve every source and span before opening the short write transaction."""
    _object(payload, "payload")
    if payload.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported knowledge candidate schema")
    source_run = _text(payload.get("source_run"), "source_run", 100)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source_run):
        raise ValueError("Invalid source_run")
    provenance = _object(payload.get("provenance", {}), "provenance")
    records = _list(payload.get("records"), "records", 200, empty=False)
    versions = MaterialVersion.objects.select_related("material").in_bulk(
        {
            _uuid(record.get("version_id"), "version_id")
            for record in records
            if isinstance(record, dict)
        }
    )
    passage_ids = set()
    for record in records:
        _object(record, "record")
        for anchor in _list(record.get("anchors"), "anchors", 10000, empty=False):
            _object(anchor, "anchor")
            passage_ids.add(_uuid(anchor.get("passage_id"), "passage_id"))
    passages = Passage.objects.in_bulk(passage_ids)
    prepared = []
    all_entities = {}
    groups = set()
    for record in records:
        group_id = _text(record.get("group_id"), "group_id", 80)
        if group_id in groups:
            raise ValueError("Duplicate group_id")
        groups.add(group_id)
        version = versions.get(_uuid(record.get("version_id"), "version_id"))
        if version is None:
            raise ValueError("Source version does not exist")
        _validate_article(record, version)
        fact = _object(record.get("fact"), "fact")
        assessment = _object(record.get("assessment", {}), "assessment")
        item_provenance = _object(record.get("provenance", {}), "record provenance")
        entities = {}
        for entity in _list(record.get("entities"), "entities", 1000):
            _object(entity, "entity")
            ref = _text(entity.get("ref"), "entity ref", 100)
            if ref in entities:
                raise ValueError("Duplicate entity ref")
            kind = entity.get("kind")
            if kind not in KINDS:
                raise ValueError("Unsupported entity kind")
            subtype = _text(entity.get("subtype", ""), "subtype", 64, empty=True)
            label = _text(entity.get("label"), "entity label", 2000)
            attributes = _object(entity.get("attributes", {}), "entity attributes")
            # Exact source spelling is deliberate: do not merge abbreviations or case variants.
            identity_key = digest([str(version.pk), kind, subtype, label])
            spec = {
                "id": uuid.uuid5(NAMESPACE, "entity:" + identity_key),
                "identity_key": identity_key,
                "scope_version_id": version.pk,
                "kind": kind,
                "subtype": subtype,
                "label": label,
                "attributes": attributes,
            }
            if identity_key in all_entities and all_entities[identity_key] != spec:
                raise ValueError("Conflicting attributes for the same scoped entity")
            all_entities[identity_key] = spec
            entities[ref] = spec
        candidate_id = uuid.uuid5(NAMESPACE, "candidate:" + source_run + ":" + group_id)
        anchors = []
        hashes = set()
        anchored_refs = set()
        for anchor in record["anchors"]:
            passage = passages.get(_uuid(anchor.get("passage_id"), "passage_id"))
            if passage is None or passage.version_id != version.pk:
                raise ValueError(
                    "Anchor passage is missing or belongs to another version"
                )
            start, end, quote = (
                anchor.get("start"),
                anchor.get("end"),
                anchor.get("quote"),
            )
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(passage.text)
                or not isinstance(quote, str)
                or passage.text[start:end] != quote
            ):
                raise ValueError("Anchor quote does not exactly match its source span")
            attributes = _object(anchor.get("attributes", {}), "anchor attributes")
            for metadata in (anchor, attributes):
                if (
                    metadata.get("parser_version") is not None
                    and metadata["parser_version"] != passage.parser_version
                ):
                    raise ValueError("Anchor parser version does not match")
                if (
                    metadata.get("locator") is not None
                    and metadata["locator"] != passage.locator
                ):
                    raise ValueError("Anchor locator does not match")
            ref = anchor.get("entity_ref") or ""
            if ref and ref not in entities:
                raise ValueError("Anchor entity ref is unknown")
            purpose, association = anchor.get("purpose"), anchor.get("association")
            if purpose not in PURPOSES or association not in ASSOCIATIONS:
                raise ValueError("Invalid anchor purpose or association")
            if purpose == "mention" and (not ref or quote != entities[ref]["label"]):
                raise ValueError("Entity mention must quote the exact entity label")
            roles = _list(anchor.get("roles", []), "roles", 30)
            for role in roles:
                _text(role, "role", 80)
            anchor_hash = digest(anchor)
            if anchor_hash in hashes:
                raise ValueError("Duplicate anchor")
            hashes.add(anchor_hash)
            if ref:
                anchored_refs.add(ref)
            anchors.append(
                {
                    "id": uuid.uuid5(NAMESPACE, str(candidate_id) + ":" + anchor_hash),
                    "candidate_id": candidate_id,
                    "entity_id": entities[ref]["id"] if ref else None,
                    "passage_id": passage.pk,
                    "content_sha256": anchor_hash,
                    "entity_ref": ref,
                    "purpose": purpose,
                    "association": association,
                    "roles": roles,
                    "start": start,
                    "end": end,
                    "quote": quote,
                    "attributes": attributes,
                }
            )
        if set(entities) - anchored_refs:
            raise ValueError(
                "Entity has no source anchor; preserve it as unresolved provenance"
            )
        prepared.append(
            {
                "id": candidate_id,
                "source_run": source_run,
                "group_id": group_id,
                "version_id": version.pk,
                "content_sha256": digest(record),
                "fact": fact,
                "assessment": assessment,
                "provenance": {"run": provenance, "record": item_provenance},
                "anchors": anchors,
            }
        )
    return {
        "source_run": source_run,
        "import_sha256": digest(payload),
        "records": prepared,
        "entities": all_entities,
        "versions": versions,
    }


def _check_existing(prepared):
    candidates = list(
        KnowledgeCandidate.objects.filter(source_run=prepared["source_run"])
    )
    if candidates:
        expected = {
            record["group_id"]: record["content_sha256"]
            for record in prepared["records"]
        }
        actual = {
            candidate.group_id: candidate.content_sha256 for candidate in candidates
        }
        if actual != expected or any(
            candidate.import_sha256 != prepared["import_sha256"]
            for candidate in candidates
        ):
            raise ValueError(
                "Existing source_run has different content; use a new version"
            )
    existing_entities = {
        entity.identity_key: entity
        for entity in KnowledgeEntity.objects.filter(
            identity_key__in=prepared["entities"]
        )
    }
    for key, entity in existing_entities.items():
        spec = prepared["entities"][key]
        if any(getattr(entity, field) != value for field, value in spec.items()):
            raise ValueError(
                "Existing scoped entity has different content; it cannot be overwritten"
            )
    return candidates, existing_entities


def import_candidates(payload, *, dry_run=False):
    from apps.curation.entity_registry_integration import register_candidates

    prepared = validate_payload(payload)
    existing, entities = _check_existing(prepared)
    result = {
        "schema_version": SCHEMA,
        "source_run": prepared["source_run"],
        "import_sha256": prepared["import_sha256"],
        "dry_run": dry_run,
        "reused": bool(existing),
        "candidate_count": len(prepared["records"]),
        "entity_count": len(prepared["entities"]),
        "anchor_count": sum(len(item["anchors"]) for item in prepared["records"]),
        "new_entity_count": len(prepared["entities"]) - len(entities),
        "candidates": [
            {"id": str(item["id"]), "group_id": item["group_id"]}
            for item in prepared["records"]
        ],
        "status": "research_candidate",
    }
    if dry_run:
        return result
    if existing:
        result["entity_registry"] = register_candidates([item.pk for item in existing])
        return result
    with transaction.atomic():
        # Recheck mutable source integrity and concurrent imports inside the write boundary.
        valid_versions = MaterialVersion.objects.filter(
            pk__in=prepared["versions"], integrity_status="valid"
        ).count()
        if valid_versions != len(prepared["versions"]):
            raise ValueError("Source integrity changed before import")
        existing, entities = _check_existing(prepared)
        if existing:
            result["reused"] = True
            result["new_entity_count"] = 0
        else:
            KnowledgeEntity.objects.bulk_create(
                [
                    KnowledgeEntity(**spec)
                    for key, spec in prepared["entities"].items()
                    if key not in entities
                ]
            )
            KnowledgeCandidate.objects.bulk_create(
                [
                    KnowledgeCandidate(
                        **{
                            key: value
                            for key, value in item.items()
                            if key != "anchors"
                        },
                        import_sha256=prepared["import_sha256"],
                    )
                    for item in prepared["records"]
                ]
            )
            KnowledgeAnchor.objects.bulk_create(
                [
                    KnowledgeAnchor(**anchor)
                    for item in prepared["records"]
                    for anchor in item["anchors"]
                ],
                batch_size=100,
            )
    result["entity_registry"] = register_candidates(
        [item["id"] for item in prepared["records"]]
    )
    return result
