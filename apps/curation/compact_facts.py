"""Deterministic assembly of compact research facts, never semantic repair.

The frozen paragraph packet remains unchanged. Units are a derived, lossless
view; their offsets count Unicode code points in parsed paragraphs. Citation
binding establishes location only, not medical entailment or correctness.
"""

import re
from copy import deepcopy
from itertools import pairwise

from apps.curation.knowledge_candidates import (
    ENTITY_TYPES,
    ENUMS,
    OPTIONAL_TEXT,
    TEXT_ARRAYS,
    validate_candidate,
)

VERSION = "compact-fact-assembly-v1"
FACT_KEYS = {"claim", "entities", *OPTIONAL_TEXT, *ENUMS, *TEXT_ARRAYS}
ROLE_FIELDS = {
    "disease": "disease_ids",
    "intervention": "intervention_ids",
    "comparator": "comparator_ids",
    "molecular": "molecular_ids",
}
ENTITY_ROLES = {*ROLE_FIELDS, "context"}
CHECK_VALUES = {"pass", "fail", "not_applicable", "uncertain"}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _error(errors, code, path, message):
    errors.append({"code": code, "path": path, "message": message})


def _keys(value, expected, path, errors):
    if not isinstance(value, dict):
        _error(errors, "object_required", path, "Expected a JSON object.")
        return False
    if value.keys() != expected:
        _error(
            errors,
            "object_keys",
            path,
            f"Expected exactly these keys: {sorted(expected)}.",
        )
        return False
    return True


def _string_list(value, path, errors, allowed=None):
    if not isinstance(value, list) or any(not _text(item) for item in value):
        _error(errors, "string_array_required", path, "Expected nonempty strings.")
        return False
    if len(value) != len(set(value)):
        _error(errors, "duplicate_values", path, "Duplicate values are not allowed.")
    if allowed is not None and not set(value) <= allowed:
        _error(errors, "unknown_value", path, "Contains an unknown value.")
    return True


def _pair(value, path, errors, *, nullable=False, enum=None):
    if not isinstance(value, list) or len(value) != 2:
        _error(errors, "support_pair_required", path, "Expected [value, [unit IDs]].")
        return
    text, references = value
    references_ok = _string_list(references, f"{path}[1]", errors)
    if enum is not None:
        if not isinstance(text, str) or text not in enum:
            _error(errors, "invalid_enum", f"{path}[0]", "Unknown enumeration value.")
    elif text is None and nullable:
        if references_ok and references:
            _error(
                errors, "null_with_support", path, "Null text requires empty support."
            )
    elif not _text(text):
        _error(errors, "text_required", f"{path}[0]", "Expected nonempty text.")


def _compact_errors(compact):
    errors = []
    if not _keys(compact, {"fact", "reason"}, "compact", errors):
        return errors
    fact = compact["fact"]
    if fact is None:
        if not _text(compact["reason"]):
            _error(
                errors, "abstention_reason_required", "compact.reason", "Give a reason."
            )
        return errors
    if compact["reason"] is not None:
        _error(errors, "fact_reason_must_be_null", "compact.reason", "Expected null.")
    if not _keys(fact, FACT_KEYS, "compact.fact", errors):
        return errors
    _pair(fact["claim"], "compact.fact.claim", errors)
    for key in sorted(OPTIONAL_TEXT):
        _pair(fact[key], f"compact.fact.{key}", errors, nullable=True)
    for key, values in ENUMS.items():
        _pair(fact[key], f"compact.fact.{key}", errors, enum=values)
    for key in sorted(TEXT_ARRAYS):
        if not isinstance(fact[key], list):
            _error(
                errors, "array_required", f"compact.fact.{key}", "Expected an array."
            )
        else:
            for index, value in enumerate(fact[key]):
                _pair(value, f"compact.fact.{key}[{index}]", errors)
    entities = fact["entities"]
    if not isinstance(entities, list) or len(entities) > 14:
        _error(
            errors, "entities_array", "compact.fact.entities", "At most 14 entities."
        )
        return errors
    for index, entity in enumerate(entities):
        path = f"compact.fact.entities[{index}]"
        if not isinstance(entity, list) or len(entity) != 4:
            _error(
                errors,
                "entity_tuple_required",
                path,
                "Expected [type, text, unit, roles].",
            )
            continue
        kind, text, unit_id, roles = entity
        if not isinstance(kind, str) or kind not in ENTITY_TYPES:
            _error(errors, "invalid_entity_type", f"{path}[0]", "Unknown entity type.")
        if not _text(text):
            _error(
                errors, "text_required", f"{path}[1]", "Expected exact mention text."
            )
        if not _text(unit_id):
            _error(errors, "unit_id_required", f"{path}[2]", "Expected a unit ID.")
        _string_list(roles, f"{path}[3]", errors, ENTITY_ROLES)
    return errors


def build_units(packet):
    """Return canonical coarse sentence units, preserving every source character.

    Whitespace following punctuation stays with the previous unit, so joining
    units of each paragraph exactly recovers its text. IDs are packet-local;
    ``assemble`` recomputes this entire list before accepting any references.
    Invalid source packets raise ValueError rather than produce trusted units.
    """
    provenance = validate_candidate(
        packet, {"candidate": None, "reason": "unit preflight"}
    )
    if not provenance["valid"]:
        codes = ", ".join(error["code"] for error in provenance["errors"])
        raise ValueError(f"Invalid frozen paragraph packet: {codes}")
    if not _text(packet.get("id")):
        raise ValueError("Frozen paragraph packet requires a nonempty ID.")
    units = []
    for passage in packet["passages"]:
        text = passage["text"]
        boundaries = [0, *[m.end() for m in re.finditer(r"(?<=[.!?。！？])\s+", text)]]
        if boundaries[-1] != len(text):
            boundaries.append(len(text))
        for start, end in pairwise(boundaries):
            units.append(
                {
                    "id": f"{packet['id']}.u{len(units) + 1}",
                    "passage_id": passage["id"],
                    "start": start,
                    "end": end,
                    "text": text[start:end],
                    "role": passage["role"],
                }
            )
    return units


def _unit_integrity(units, expected, errors):
    if not isinstance(units, list) or len(units) != len(expected):
        _error(
            errors,
            "unit_integrity",
            "units",
            "Must contain the complete derived unit list.",
        )
        return
    for index, (unit, original) in enumerate(zip(units, expected)):
        # Python's True == 1 must not let forged offsets pass strict JSON checks.
        if (
            not isinstance(unit, dict)
            or unit != original
            or type(unit.get("start")) is not int
            or type(unit.get("end")) is not int
        ):
            _error(
                errors,
                "unit_integrity",
                f"units[{index}]",
                "Unit differs from the frozen packet's canonical text/span/identity.",
            )


def assemble(packet, units, compact):
    """Assemble old-schema payload and validate it without silently fixing facts.

    ``valid`` means a non-abstained, structurally valid candidate only. It does
    not denote medical correctness. Source/compact errors yield payload=None;
    old-validator failures retain the assembled payload for diagnosis.
    """
    result = {
        "schema_version": VERSION,
        "payload": None,
        "validation": None,
        "errors": [],
        "valid": False,
        "status": "invalid",
        "unit_spans": {"entities": [], "evidence": []},
    }
    errors = result["errors"]
    try:
        expected = build_units(packet)
    except (ValueError, TypeError) as exc:
        _error(errors, "packet_integrity", "packet", str(exc))
        return result
    _unit_integrity(units, expected, errors)
    errors.extend(_compact_errors(compact))
    if errors:
        return result
    fact = compact["fact"]
    if fact is None:
        payload = {"candidate": None, "reason": compact["reason"]}
    else:
        unit_by_id = {unit["id"]: unit for unit in units}
        citations = {}

        def cite(field, unit_ids, path):
            for unit_id in unit_ids:
                if unit_id not in unit_by_id:
                    _error(errors, "unselected_unit", path, f"Unknown unit: {unit_id}.")
                else:
                    citations.setdefault(unit_id, set()).add(field)

        candidate = {"entities": [], **{key: [] for key in ROLE_FIELDS.values()}}
        for key in ["claim", *sorted(OPTIONAL_TEXT), *ENUMS]:
            value, refs = fact[key]
            field = "statement" if key == "claim" else key
            candidate[field] = value
            cite(field, refs, f"compact.fact.{key}")
        for key in sorted(TEXT_ARRAYS):
            candidate[key] = []
            for index, (value, refs) in enumerate(fact[key]):
                candidate[key].append(value)
                cite(key, refs, f"compact.fact.{key}[{index}]")
        for index, (kind, text, unit_id, roles) in enumerate(fact["entities"]):
            path = f"compact.fact.entities[{index}]"
            unit = unit_by_id.get(unit_id)
            if unit is None:
                _error(errors, "unselected_unit", path, f"Unknown unit: {unit_id}.")
                continue
            if text not in unit["text"]:
                _error(
                    errors,
                    "entity_not_in_unit",
                    path,
                    "Mention is not an exact unit substring.",
                )
                continue
            entity_id = f"e{index + 1}"
            candidate["entities"].append(
                {
                    "id": entity_id,
                    "type": kind,
                    "text": text,
                    "passage_id": unit["passage_id"],
                }
            )
            matches = []
            start = unit["text"].find(text)
            while start >= 0:
                matches.append(
                    {
                        "start": unit["start"] + start,
                        "end": unit["start"] + start + len(text),
                    }
                )
                start = unit["text"].find(text, start + 1)
            result["unit_spans"]["entities"].append(
                {
                    "entity_id": entity_id,
                    "unit_id": unit_id,
                    "passage_id": unit["passage_id"],
                    "matches": matches,
                }
            )
            for role in roles:
                if role in ROLE_FIELDS:
                    field = ROLE_FIELDS[role]
                    candidate[field].append(entity_id)
                    cite(field, [unit_id], path)
        if errors:
            return result
        candidate["evidence"] = []
        passage_texts = {
            passage["id"]: passage["text"] for passage in packet["passages"]
        }
        groups = []
        for unit in units:
            if unit["id"] not in citations:
                continue
            fields = sorted(citations[unit["id"]])
            detail = {
                "unit_id": unit["id"],
                "start": unit["start"],
                "end": unit["end"],
                "fields": fields,
            }
            previous = groups[-1] if groups else None
            gap = (
                passage_texts[unit["passage_id"]][previous["end"] : unit["start"]]
                if previous and previous["passage_id"] == unit["passage_id"]
                else None
            )
            if gap is not None and (not gap or gap.isspace()):
                previous["end"] = unit["end"]
                previous["fields"] = sorted(set(previous["fields"]) | set(fields))
                previous["units"].append(detail)
            else:
                groups.append({**deepcopy(unit), "fields": fields, "units": [detail]})
        for group in groups:
            quote = passage_texts[group["passage_id"]][group["start"] : group["end"]]
            # IDs belong to individual units; the quote may contain several.
            group.pop("id")
            group["text"] = quote
            result["unit_spans"]["evidence"].append(
                {"evidence_index": len(candidate["evidence"]), **group}
            )
            candidate["evidence"].append(
                {
                    "passage_id": group["passage_id"],
                    "quote": quote,
                    "fields": group["fields"],
                }
            )
        payload = {"candidate": candidate, "reason": None}
    result["payload"] = payload
    validation = validate_candidate(packet, payload)
    result["validation"] = validation
    result["status"] = validation["status"]
    result["valid"] = validation["valid"] and validation["status"] != "abstained"
    errors.extend(deepcopy(validation["errors"]))
    return result


def apply_review(compact, review):
    """Apply a whitelisted top-level patch to a copy; callers must reassemble.

    A valid review transaction is not a semantic approval. In particular,
    abstention returns a null fact and cannot become a successful candidate.
    """
    result = {
        "compact": None,
        "valid": False,
        "errors": [],
        "decision": None,
        "checks": None,
    }
    errors = result["errors"]
    if not _keys(review, {"decision", "checks", "reason", "patch"}, "review", errors):
        return result
    decision, checks, reason, patch = (
        review[key] for key in ("decision", "checks", "reason", "patch")
    )
    result.update(decision=deepcopy(decision), checks=deepcopy(checks))
    if decision == "revise":
        # An explicit patch may repair a malformed value (e.g. entity type),
        # but cannot invent/remove fields or turn an unparseable object into a
        # fact. The merged output receives full shape and source validation.
        if _keys(compact, {"fact", "reason"}, "compact", errors):
            _keys(compact["fact"], FACT_KEYS, "compact.fact", errors)
    else:
        errors.extend(_compact_errors(compact))
    if not isinstance(decision, str) or decision not in {"accept", "revise", "abstain"}:
        _error(
            errors, "invalid_decision", "review.decision", "Unknown review decision."
        )
    checks_valid = _keys(
        checks, {"groups", "conditions", "numbers"}, "review.checks", errors
    )
    if checks_valid:
        for key, value in checks.items():
            if not isinstance(value, str) or value not in CHECK_VALUES:
                _error(
                    errors,
                    "invalid_check",
                    f"review.checks.{key}",
                    "Unknown check result.",
                )
    if reason is not None and not _text(reason):
        _error(
            errors, "invalid_reason", "review.reason", "Expected nonempty text or null."
        )
    if decision == "accept":
        if patch is not None:
            _error(
                errors,
                "accept_with_patch",
                "review.patch",
                "Acceptance cannot modify facts.",
            )
        if checks_valid and any(
            value not in ("pass", "not_applicable") for value in checks.values()
        ):
            _error(
                errors,
                "unresolved_accept",
                "review.checks",
                "Cannot accept failed or uncertain checks.",
            )
        if isinstance(compact, dict) and compact.get("fact") is None:
            _error(
                errors,
                "accept_abstention",
                "compact.fact",
                "A null fact cannot be accepted.",
            )
    elif decision == "revise":
        if not isinstance(patch, dict) or not patch:
            _error(
                errors,
                "patch_required",
                "review.patch",
                "Revision requires a nonempty patch.",
            )
        elif not set(patch) <= FACT_KEYS:
            _error(
                errors,
                "patch_keys",
                "review.patch",
                "Only existing top-level fact fields may change.",
            )
        if isinstance(compact, dict) and not isinstance(compact.get("fact"), dict):
            _error(
                errors,
                "revise_abstention",
                "compact.fact",
                "A null fact has no fields to patch.",
            )
    elif decision == "abstain":
        if patch is not None:
            _error(
                errors,
                "abstain_with_patch",
                "review.patch",
                "Abstention requires patch=null.",
            )
        if not _text(reason):
            _error(
                errors,
                "abstention_reason_required",
                "review.reason",
                "Give an abstention reason.",
            )
    if errors:
        return result
    updated = deepcopy(compact)
    if decision == "revise":
        updated["fact"].update(deepcopy(patch))
    elif decision == "abstain":
        updated = {"fact": None, "reason": reason}
    errors.extend(_compact_errors(updated))
    if not errors:
        result.update(compact=updated, valid=True)
    return result
