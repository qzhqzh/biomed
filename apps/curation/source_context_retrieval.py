"""Find additional source passages for a previously extracted result.

This is bounded to one verified article. A lexical hit supplies context for a
subsequent model, never a fact, a source-support judgment or target approval.
"""

import re
import uuid

from apps.curation.source_linked_facts import evidence_index

VERSION = "biomed.source-context-retrieval.v1"


def _contains(text, term):
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE))


def _uuid(value):
    try:
        return uuid.UUID(str(value)).hex
    except (TypeError, ValueError, AttributeError):
        return None


def propose_support(sample, *, variant, drug, limit=2):
    """Locate exact variant, drug and result value in additional source passages."""
    if not isinstance(variant, str) or not variant.strip() or not isinstance(drug, str) or not drug.strip():
        raise ValueError("Specific variant and drug text are required")
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Context retrieval limit must be one to three passages")
    index = evidence_index(sample["record"], sample["source_document"])
    existing = {_uuid(item["passage_id"]) for item in index.values()}
    fact = sample["record"].get("fact") or {}
    results = fact.get("results") if isinstance(fact.get("results"), list) else []
    # Match the previously reported result literally; no numeric rewriting,
    # extrapolation or searching for a value made up by this helper.
    values = [r["value"].strip() for r in results if isinstance(r, dict)
              and isinstance(r.get("value"), str) and len(r["value"].strip()) >= 3]
    version = _uuid(sample["record"].get("version_id"))
    candidates = []
    for passage in sample["source_document"]["passages"]:
        pid, text = _uuid(passage.get("id")), passage.get("text")
        if (not pid or pid in existing or _uuid(passage.get("version_id")) != version
                or not isinstance(text, str) or not _contains(text, variant) or not _contains(text, drug)):
            continue
        found = [value for value in values if value.casefold() in text.casefold()]
        if not found:
            continue
        candidates.append({"passage_id": str(passage["id"]), "locator": passage.get("locator"),
                           "version_id": sample["record"]["version_id"],
                           "start": 0, "end": len(text), "quote": text,
                           "matched_variant": variant, "matched_drug": drug, "matched_result_values": found})
    return {"version": VERSION, "group_id": sample["record"]["group_id"],
            "article_source_sha256": sample["record"]["source_sha256"], "candidates": candidates[:limit],
            "additional_matches_not_returned": max(0, len(candidates) - limit),
            "semantic_support_verified": False, "target_approved": False,
            "scope_note": "Exact mentions in one paragraph do not prove the same cohort or the clinical relation."}
