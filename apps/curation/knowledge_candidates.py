"""Offline structural checks for source-grounded research candidates.

Exact quotes establish location, not entailment or clinical correctness. Offsets
refer to immutable parsed paragraph text (Unicode code points), not raw XML.
This module does not normalize entities, grade evidence or publish knowledge.
"""

import re
from copy import deepcopy

from apps.curation.decision import digest

VERSION = "knowledge-candidate-validation-v1"
ENTITY_TYPES = {
    "disease",
    "drug",
    "gene",
    "protein",
    "variant",
    "biomarker",
    "treatment",
}
REFERENCE_TYPES = {
    "disease_ids": {"disease"},
    "intervention_ids": {"drug", "treatment"},
    # The extraction contract does not restrict the comparator entity family.
    "comparator_ids": ENTITY_TYPES,
    "molecular_ids": {"gene", "protein", "variant", "biomarker"},
}
ENUMS = {
    "molecular_logic": {"none", "single", "and", "or", "complex", "unspecified"},
    "direction": {
        "benefit",
        "harm",
        "no_significant_difference",
        "resistance",
        "association",
        "recommendation",
        "mixed",
        "unclear",
    },
    "study_scope": {"human", "animal", "cell", "mixed", "unclear"},
    "source_role": {
        "current_study",
        "cited_study",
        "recommendation_or_regulatory_report",
        "unclear",
    },
}
OPTIONAL_TEXT = {"population", "regimen", "comparison", "outcome"}
TEXT_ARRAYS = {"conditions", "limitations"}
CLAIM_FIELDS = {"statement", *REFERENCE_TYPES, *ENUMS, *OPTIONAL_TEXT, *TEXT_ARRAYS}
CANDIDATE_KEYS = CLAIM_FIELDS | {"entities", "evidence"}
ARTICLE_KEYS = {
    "pmid",
    "pmcid",
    "doi",
    "title",
    "material_id",
    "version_id",
    "source_sha256",
    "source_url",
    "rights",
    "acquired_at",
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _matches(text, quote):
    """Return every occurrence, including overlaps; never pick an arbitrary one."""
    matches = []
    start = text.find(quote)
    while start >= 0:
        matches.append({"start": start, "end": start + len(quote)})
        start = text.find(quote, start + 1)
    return matches


def validate_candidate(packet, payload):
    """Validate a parsed JSON payload against a frozen selected-paragraph packet.

    All failures are returned, not silently repaired. ``valid`` denotes schema
    and source-location checks only. Packet hashes detect accidental changes;
    callers must authenticate the frozen input/run manifest separately.
    """
    result = {
        "schema_version": VERSION,
        "packet_id": packet.get("id") if isinstance(packet, dict) else None,
        "status": "invalid",
        "valid": False,
        "semantic_status": "not_assessed",
        "article_provenance": None,
        "entity_spans": [],
        "evidence_spans": [],
        "field_supports": {},
        "errors": [],
        "warnings": [],
    }

    def error(code, path, message):
        result["errors"].append({"code": code, "path": path, "message": message})

    def keys(value, expected, path):
        if not isinstance(value, dict):
            error("object_required", path, "Expected a JSON object.")
            return False
        missing, extra = expected - value.keys(), value.keys() - expected
        if missing or extra:
            error(
                "object_keys",
                path,
                f"Missing keys: {sorted(missing)}; extra keys: {sorted(extra)}.",
            )
        return True

    def string_list(value, path):
        if not isinstance(value, list) or any(not _text(item) for item in value):
            error(
                "string_array_required", path, "Expected an array of nonempty strings."
            )
            return False
        if len(value) != len(set(value)):
            error("duplicate_values", path, "Duplicate values are not allowed.")
        return True

    if not isinstance(packet, dict):
        error("packet_object_required", "packet", "Expected a frozen packet object.")
        return result
    expected_hash = packet.get("input_sha256")
    try:
        actual_hash = digest({k: v for k, v in packet.items() if k != "input_sha256"})
    except (TypeError, ValueError):
        actual_hash = None
    if not _text(expected_hash) or expected_hash != actual_hash:
        error(
            "packet_hash_mismatch",
            "packet.input_sha256",
            "Frozen packet digest differs.",
        )
    article = packet.get("article")
    if not isinstance(article, dict):
        error("article_required", "packet.article", "Article provenance is required.")
        article = {}
    result["article_provenance"] = {
        k: deepcopy(article.get(k)) for k in sorted(ARTICLE_KEYS)
    }
    for name in (
        "pmid",
        "title",
        "material_id",
        "version_id",
        "source_sha256",
        "source_url",
        "acquired_at",
    ):
        if not _text(article.get(name)):
            error(
                "article_field_required",
                f"packet.article.{name}",
                "Nonempty provenance field required.",
            )
    if article.get("pmid") != packet.get("article_pmid"):
        error(
            "article_mismatch", "packet.article_pmid", "Packet and article PMID differ."
        )
    if not re.fullmatch(r"[a-f0-9]{64}", str(article.get("source_sha256", ""))):
        error(
            "invalid_source_hash",
            "packet.article.source_sha256",
            "Expected a SHA-256 digest.",
        )

    passage_list = packet.get("passages")
    if not isinstance(passage_list, list) or not passage_list:
        error(
            "passages_required",
            "packet.passages",
            "At least one selected paragraph required.",
        )
        passage_list = []
    passages = {}
    for index, passage in enumerate(passage_list):
        path = f"packet.passages[{index}]"
        if not isinstance(passage, dict) or not _text(passage.get("id")):
            error("invalid_passage", path, "Paragraph needs an ID and provenance.")
            continue
        passage_id = passage["id"]
        if passage_id in passages:
            error("duplicate_passage_id", path, "Paragraph IDs must be unique.")
            continue
        passages[passage_id] = passage
        for key in ("pmid", "version_id", "source_sha256"):
            if passage.get(key) != article.get(key):
                error(
                    "passage_article_mismatch",
                    f"{path}.{key}",
                    "Paragraph belongs to a different article/source version.",
                )
        for key in (
            "text",
            "locator",
            "parser_version",
            "input_sha256",
            "upstream_decision_sha256",
        ):
            if not _text(passage.get(key)):
                error(
                    "passage_field_required",
                    f"{path}.{key}",
                    "Nonempty paragraph provenance required.",
                )
        if passage.get("role") not in ("anchor", "support"):
            error("invalid_passage_role", f"{path}.role", "Expected anchor or support.")
    anchor_id = packet.get("anchor_id")
    if (
        not isinstance(anchor_id, str)
        or anchor_id not in passages
        or passages[anchor_id].get("role") != "anchor"
    ):
        error(
            "invalid_anchor",
            "packet.anchor_id",
            "Anchor must identify a selected anchor paragraph.",
        )
    if sum(p.get("role") == "anchor" for p in passages.values()) != 1:
        error(
            "anchor_count", "packet.passages", "Exactly one anchor paragraph required."
        )

    def span(passage_id, quote, path):
        if not isinstance(passage_id, str) or passage_id not in passages:
            error(
                "unselected_passage",
                path,
                "Only selected packet paragraphs may be cited.",
            )
            return None
        if not _text(quote):
            error(
                "quote_required",
                path,
                "An exact nonempty source substring is required.",
            )
            return None
        passage = passages[passage_id]
        positions = (
            _matches(passage.get("text", ""), quote)
            if isinstance(passage.get("text"), str)
            else []
        )
        if not positions:
            error(
                "quote_not_found",
                path,
                "Text is not an exact contiguous source substring.",
            )
            return None
        item = {
            "passage_id": passage_id,
            "pmid": passage.get("pmid"),
            "version_id": passage.get("version_id"),
            "source_sha256": passage.get("source_sha256"),
            "parser_version": passage.get("parser_version"),
            "locator": passage.get("locator"),
            "ordinal": passage.get("ordinal"),
            "passage_input_sha256": passage.get("input_sha256"),
            "upstream_decision_sha256": passage.get("upstream_decision_sha256"),
            "role": passage.get("role"),
            "quote": quote,
            "coordinate_space": "parsed_passage_unicode_codepoints",
            "matches": positions,
            "ambiguous": len(positions) > 1,
        }
        if item["ambiguous"]:
            result["warnings"].append(
                {
                    "code": "multiple_exact_occurrences",
                    "path": path,
                    "occurrences": len(positions),
                }
            )
        return item

    if not keys(payload, {"candidate", "reason"}, "payload"):
        return result
    candidate = payload.get("candidate")
    if candidate is None:
        if not _text(payload.get("reason")):
            error(
                "abstention_reason_required",
                "payload.reason",
                "Abstention needs a nonempty reason.",
            )
        if not result["errors"]:
            result.update(
                valid=True, status="abstained", abstention_reason=payload["reason"]
            )
        return result
    if payload.get("reason") is not None:
        error(
            "candidate_reason_must_be_null",
            "payload.reason",
            "A non-null candidate requires reason=null.",
        )
    if not keys(candidate, CANDIDATE_KEYS, "candidate"):
        return result
    if not _text(candidate.get("statement")):
        error(
            "statement_required",
            "candidate.statement",
            "A nonempty statement is required.",
        )
    for name in OPTIONAL_TEXT:
        if candidate.get(name) is not None and not _text(candidate[name]):
            error(
                "nullable_string_required",
                f"candidate.{name}",
                "Expected a nonempty string or null.",
            )
    for name, values in ENUMS.items():
        value = candidate.get(name)
        if not isinstance(value, str) or value not in values:
            error(
                "invalid_enum",
                f"candidate.{name}",
                f"Expected one of {sorted(values)}.",
            )
    for name in TEXT_ARRAYS:
        string_list(candidate.get(name), f"candidate.{name}")

    entities = candidate.get("entities")
    if not isinstance(entities, list) or len(entities) > 14:
        error(
            "entities_array",
            "candidate.entities",
            "Expected an array with at most 14 entities.",
        )
        entities = []
    entity_types = {}
    for index, entity in enumerate(entities):
        path = f"candidate.entities[{index}]"
        if not keys(entity, {"id", "type", "text", "passage_id"}, path):
            continue
        entity_id, entity_type = entity.get("id"), entity.get("type")
        if not isinstance(entity_id, str) or not re.fullmatch(
            r"e[1-9][0-9]*", entity_id
        ):
            error("invalid_entity_id", f"{path}.id", "Expected e1/e2/... entity ID.")
        elif entity_id in entity_types:
            error("duplicate_entity_id", f"{path}.id", "Entity IDs must be unique.")
        else:
            entity_types[entity_id] = entity_type
        if not isinstance(entity_type, str) or entity_type not in ENTITY_TYPES:
            error("invalid_entity_type", f"{path}.type", "Unknown entity type.")
        item = span(entity.get("passage_id"), entity.get("text"), path)
        if item:
            result["entity_spans"].append(
                {"entity_id": entity_id, "entity_type": entity_type, **item}
            )
    for name, allowed_types in REFERENCE_TYPES.items():
        values = candidate.get(name)
        if string_list(values, f"candidate.{name}"):
            for entity_id in values:
                if entity_id not in entity_types:
                    error(
                        "dangling_entity_id",
                        f"candidate.{name}",
                        f"Unknown entity ID: {entity_id}.",
                    )
                elif (
                    not isinstance(entity_types[entity_id], str)
                    or entity_types[entity_id] not in allowed_types
                ):
                    error(
                        "reference_entity_type",
                        f"candidate.{name}",
                        f"Incompatible entity type for {entity_id}.",
                    )

    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 6:
        error(
            "evidence_array",
            "candidate.evidence",
            "Expected between one and six exact evidence quotes.",
        )
        evidence = []
    anchor_fields = set()
    for index, evidence_item in enumerate(evidence):
        path = f"candidate.evidence[{index}]"
        if not keys(evidence_item, {"passage_id", "quote", "fields"}, path):
            continue
        fields = evidence_item.get("fields")
        good_fields = string_list(fields, f"{path}.fields")
        if good_fields and (not fields or not set(fields) <= CLAIM_FIELDS):
            error(
                "invalid_evidence_fields",
                f"{path}.fields",
                "Use nonempty exact claim field names; vague or entity-wide tags are not allowed.",
            )
            good_fields = False
        item = span(evidence_item.get("passage_id"), evidence_item.get("quote"), path)
        if item and good_fields:
            result["evidence_spans"].append(
                {"evidence_index": index, "fields": fields[:], **item}
            )
            for field in fields:
                result["field_supports"].setdefault(field, []).append(index)
            if evidence_item.get("passage_id") == anchor_id:
                anchor_fields.update(fields)
    required_fields = {"statement"}
    for name in REFERENCE_TYPES.keys() | OPTIONAL_TEXT | TEXT_ARRAYS:
        if candidate.get(name):
            required_fields.add(name)
    for name in ENUMS:
        if candidate.get(name) not in ("none", "unspecified", "unclear"):
            required_fields.add(name)
    for field in sorted(required_fields - result["field_supports"].keys()):
        error(
            "missing_field_support",
            f"candidate.{field}",
            "Populated claim field lacks a valid exact source citation.",
        )
    for field in ("statement", "outcome"):
        if candidate.get(field) and field not in anchor_fields:
            error(
                "missing_anchor_support",
                f"candidate.{field}",
                "Principal statement/outcome must cite exact ANCHOR text.",
            )
    if not result["errors"]:
        result.update(valid=True, status="structurally_valid_candidate")
    return result
