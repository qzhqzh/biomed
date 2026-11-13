"""Defer an unlocated extra biomarker reference without changing medical text.

This is protocol adaptation, not a judgment of medical irrelevance. Existing
semantic review and all other structural checks remain necessary.
"""

from copy import deepcopy

VERSION = "core-knowledge-optional-entities-v1"
TEXT_FIELDS = (
    "statement",
    "population",
    "treatment",
    "comparator",
    "conditions",
    "results",
    "scope",
    "drug_names",
)
SUPPORT_TYPES = {"drug", "disease", "gene"}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _terms(entity):
    return list(
        dict.fromkeys(
            text
            for value in (entity.get("text"), entity.get("aliases", []))
            for text in _strings(value)
            if text.strip()
        )
    )


def _located(entity, passages):
    terms = _terms(entity)
    for loc in entity.get("locations", []):
        text = passages.get(loc.get("passage_id"))
        start, end = loc.get("start"), loc.get("end")
        if (
            isinstance(text, str)
            and type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(text)
            and text[start:end] in terms
        ):
            return True
    return False


def _mentioned(terms, texts):
    return any(term.casefold() in text.casefold() for term in terms for text in texts)


def defer_optional_entities(packet, result):
    """Return an independent view with eligible references deferred, not erased."""
    output = {
        "version": VERSION,
        "result_view": deepcopy(result),
        "unresolved_entity_refs": [],
        "changes": [],
        "kept_blockers": [],
    }
    payload = output["result_view"].get("payload")
    fact = payload.get("fact") if isinstance(payload, dict) else None
    ids = fact.get("entity_ids") if isinstance(fact, dict) else None
    if not isinstance(ids, list) or any(not isinstance(eid, str) for eid in ids):
        return output  # Existing structural validation handles malformed fields.
    entities = {e["id"]: e for e in packet["entities"]}
    passages = {p["id"]: p["text"] for p in packet["passages"]}
    texts = [text for field in TEXT_FIELDS for text in _strings(fact.get(field))]
    support = [
        eid
        for eid in dict.fromkeys(ids)
        if eid in entities
        and entities[eid].get("type") in SUPPORT_TYPES
        and _located(entities[eid], passages)
        and _mentioned(_terms(entities[eid]), texts)
    ]
    deferred = set()
    for eid in dict.fromkeys(ids):
        entity = entities.get(eid)
        if entity is not None and _located(entity, passages):
            continue
        if entity is None:
            reason = "unknown_entity_id"
        elif entity.get("type") != "biomarker":
            reason = "unlocated_core_or_unapproved_entity_type"
        elif not _terms(entity):
            reason = "missing_entity_text"
        elif _mentioned(_terms(entity), texts):
            reason = "unlocated_reference_in_fact_text"
        elif entity.get("locations"):
            reason = "invalid_source_locations"
        elif _mentioned(_terms(entity), passages.values()):
            reason = "source_mention_requires_location"
        elif not support:
            reason = "no_located_core_entity_support"
        else:
            deferred.add(eid)
            output["unresolved_entity_refs"].append(
                {
                    "entity_id": eid,
                    "entity": deepcopy(entity),
                    "reason": "unlocated_additional_reference",
                }
            )
            output["changes"].append(
                {
                    "entity_id": eid,
                    "path": "payload.fact.entity_ids",
                    "action": "defer_optional_reference",
                    "core_source_entity_ids": support,
                    "semantic_relevance": "not_established_by_adapter",
                }
            )
            continue
        output["kept_blockers"].append({"entity_id": eid, "reason": reason})
    fact["entity_ids"] = [eid for eid in ids if eid not in deferred]
    return output
