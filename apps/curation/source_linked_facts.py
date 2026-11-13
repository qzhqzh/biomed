"""Mechanically bind opt-in model facts to immutable article passages.

This records what the extraction model asserted; matching source spans do not
establish medical correctness. It neither calls models nor contacts a target.
"""

import copy
import re
import unicodedata
import uuid

from apps.curation.decision import digest as source_digest

VERSION = "biomed.source-linked-facts.v1"
RELATIONS = {"treatment_outcome", "diagnostic", "prognostic", "mechanism", "other"}
SOURCE_SCOPES = {"primary", "cited_work", "protocol", "unknown"}
EVENT_TYPES = {"mutation", "cnv", "fusion"}


def _uuid_key(value):
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return None


def _text_key(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
                  .translate(str.maketrans("‐‑‒–—", "-----"))).strip().casefold()


def evidence_index(record, document):
    """Verify source version, content hash, passage identity, offsets and text."""
    version = _uuid_key(record.get("version_id"))
    if not version or version != _uuid_key(document.get("fulltext_version_id")):
        raise ValueError("Article and fulltext version differ")
    versions = [v for v in document.get("versions", []) if _uuid_key(v.get("id")) == version]
    if len(versions) != 1 or versions[0].get("sha256") != record.get("source_sha256"):
        raise ValueError("Article content hash differs")
    passages = document.get("passages")
    if not isinstance(passages, list) or source_digest(passages) != document.get("passages_sha256"):
        raise ValueError("Article passages differ from the frozen snapshot")
    by_id = {_uuid_key(p.get("id")): p for p in passages}
    if None in by_id or len(by_id) != len(passages):
        raise ValueError("Source passages have invalid or duplicate identifiers")
    index = {}
    for anchor in record.get("anchors", []):
        if anchor.get("purpose") != "evidence":
            continue
        alias = (anchor.get("attributes") or {}).get("alias")
        passage = by_id.get(_uuid_key(anchor.get("passage_id")))
        start, end, quote = anchor.get("start"), anchor.get("end"), anchor.get("quote")
        if (not isinstance(alias, str) or not re.fullmatch(r"P[1-9][0-9]*", alias)
                or alias in index or not passage or not isinstance(passage.get("text"), str)
                or _uuid_key(passage.get("version_id")) != version
                or type(start) is not int or type(end) is not int
                or not isinstance(quote, str) or not 0 <= start < end <= len(passage["text"])
                or passage["text"][start:end] != quote):
            raise ValueError("Evidence alias, passage, offsets or text differ")
        index[alias] = {"passage_id": str(anchor["passage_id"]), "locator": passage.get("locator"),
                        "start": start, "end": end, "quote": quote}
    if not index:
        raise ValueError("No valid source evidence")
    return index


def _resolved_evidence(value, index, path, issues, *, required=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in index for v in value):
        issues.append({"code": "invalid_evidence_reference", "path": path})
        return []
    if len(value) != len(set(value)):
        issues.append({"code": "duplicate_evidence_reference", "path": path})
        return []
    if required and not value:
        issues.append({"code": "missing_evidence_reference", "path": path})
    return [copy.deepcopy(index[v]) for v in value]


def _entity(ref, entities, permitted, path, issues):
    if not isinstance(ref, str) or ref not in entities or entities[ref].get("kind") not in permitted:
        issues.append({"code": "entity_reference_unresolved", "path": path})
        return None
    return entities[ref]


def _mentioned(label, evidence):
    value = _text_key(label)
    return bool(value) and any(value in _text_key(item.get("quote")) for item in evidence)


def normalize(sample, payload):
    """Create a versioned fact and field-level source links without changing input.

    Missing or ambiguous claims are preserved as issues. The result is a
    mechanical candidate, not independent semantic review or target approval.
    """
    record, document = sample["record"], sample["source_document"]
    index = evidence_index(record, document)
    entities = {e.get("ref"): e for e in record.get("entities", []) if isinstance(e, dict) and e.get("ref")}
    if len(entities) != len(record.get("entities", [])):
        raise ValueError("Entity references are missing or duplicated")
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list) or len(payload["facts"]) > 1:
        raise ValueError("Expected at most one fact")
    if not payload["facts"]:
        return {"version": VERSION, "group_id": record["group_id"], "status": "no_fact",
                "reason": payload.get("reason"), "issues": [], "semantic_status": "unreviewed"}
    fact = payload["facts"][0]
    if not isinstance(fact, dict):
        raise TypeError("Fact must be an object")
    issues = []
    if fact.get("relation") not in RELATIONS:
        issues.append({"code": "relation_unresolved", "path": "relation"})
    if fact.get("source_scope") not in SOURCE_SCOPES:
        issues.append({"code": "source_scope_unresolved", "path": "source_scope"})

    fields = {}
    for name in ("population", "disease", "outcome"):
        value = fact.get(name)
        if not isinstance(value, dict):
            issues.append({"code": "field_unresolved", "path": name})
            fields[name] = {"text": None, "evidence": []}
            continue
        text = value.get("text")
        if text is not None and not isinstance(text, str):
            issues.append({"code": "invalid_text", "path": f"{name}.text"})
        evidence = _resolved_evidence(value.get("evidence"), index, f"{name}.evidence", issues,
                                      required=bool(text))
        fields[name] = {"text": text, "evidence": evidence}
        if name == "disease" and text:
            entity = _entity(value.get("entity_ref"), entities, {"disease"}, "disease.entity_ref", issues)
            fields[name]["entity_ref"] = value.get("entity_ref")
            if entity and not _mentioned(entity.get("label"), evidence):
                issues.append({"code": "entity_not_in_field_evidence", "path": "disease.entity_ref"})

    for name in ("molecular_event", "intervention"):
        value = fact.get(name)
        if value is None:
            fields[name] = None
            continue
        if not isinstance(value, dict):
            issues.append({"code": "field_unresolved", "path": name})
            fields[name] = None
            continue
        value = copy.deepcopy(value)
        value["evidence"] = _resolved_evidence(value.get("evidence"), index, f"{name}.evidence", issues, required=True)
        if name == "molecular_event":
            if value.get("event_type") not in EVENT_TYPES or value.get("status") not in {"present", "absent", "uncertain"}:
                issues.append({"code": "molecular_event_unresolved", "path": name})
            if value.get("role") not in {"eligibility", "outcome", "background", "unknown"}:
                issues.append({"code": "molecular_role_unresolved", "path": f"{name}.role"})
            refs = value.get("entity_refs")
            if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
                    or len(refs) != len(set(refs))):
                issues.append({"code": "entity_reference_unresolved", "path": f"{name}.entity_refs"})
                refs = []
            for ref in refs:
                _entity(ref, entities, {"gene", "variant", "biomarker"}, f"{name}.entity_refs", issues)
            for key in ("gene", "event"):
                if value.get(key) and not _mentioned(value[key], value["evidence"]):
                    issues.append({"code": "event_not_in_field_evidence", "path": f"{name}.{key}"})
        else:
            if value.get("role") not in {"current", "prior", "comparator", "unknown"}:
                issues.append({"code": "intervention_role_unresolved", "path": f"{name}.role"})
            entity = _entity(value.get("entity_ref"), entities, {"drug", "treatment"}, "intervention.entity_ref", issues)
            if entity and not _mentioned(entity.get("label"), value["evidence"]):
                issues.append({"code": "entity_not_in_field_evidence", "path": "intervention.entity_ref"})
        fields[name] = value

    same_cohort = fact.get("same_cohort")
    if same_cohort is not None and type(same_cohort) is not bool:
        issues.append({"code": "invalid_cohort_binding", "path": "same_cohort"})
    cohort_evidence = _resolved_evidence(fact.get("cohort_evidence"), index, "cohort_evidence", issues,
                                        required=same_cohort is True)
    if same_cohort is True and (fields["molecular_event"] is None or fields["intervention"] is None):
        issues.append({"code": "incomplete_cohort_binding", "path": "same_cohort"})
    conditions = fact.get("conditions")
    if not isinstance(conditions, list) or any(not isinstance(c, str) for c in conditions):
        issues.append({"code": "conditions_unresolved", "path": "conditions"})
        conditions = []
    return {"version": VERSION, "group_id": record["group_id"],
            "article": {"pmid": sample["pmid"], "version_id": record["version_id"],
                        "source_sha256": record["source_sha256"]},
            "status": "incomplete" if issues else "source_linked",
            "relation": fact.get("relation"), **fields, "same_cohort": same_cohort,
            "cohort_evidence": cohort_evidence, "conditions": conditions,
            "source_scope": fact.get("source_scope"), "issues": issues,
            "semantic_status": "unreviewed", "target_contacted": False, "target_written": False}
