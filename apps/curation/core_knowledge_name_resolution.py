"""Align case-only drug spellings to existing, literal source entities.

This creates an inspectable view, not a confirmed standard identity. Aliases,
medical free text and original model/entity records are never rewritten.
"""

from copy import deepcopy

from apps.curation.core_knowledge_intake import _drug_locations
from apps.curation.core_knowledge_projection import _kind

VERSION = "core-knowledge-name-resolution-v1"


def _identity_issue(entities):
    if not entities:
        return "no_existing_drug_entity"
    if any(e.get("type") not in {None, "drug"} for e in entities):
        return "non_drug_or_mixed_entity_types"
    if any(_kind([e]) != "drug" for e in entities):
        return "ambiguous_or_unproven_drug_identity"
    linked_ids = {}
    for entity in entities:
        candidate_id = (entity.get("link") or {}).get("candidate_id")
        if isinstance(candidate_id, str) and ":" in candidate_id:
            namespace = candidate_id.split(":", 1)[0]
            linked_ids.setdefault(namespace, set()).add(candidate_id)
    if any(len(ids) > 1 for ids in linked_ids.values()):
        return "conflicting_linked_ids"
    return None


def resolve(packet, result):
    """Return result_view plus case-only changes and unresolved selected names."""
    output = {
        "version": VERSION,
        "result_view": deepcopy(result),
        "changes": [],
        "unresolved": [],
    }
    payload = output["result_view"].get("payload")
    fact = payload.get("fact") if isinstance(payload, dict) else None
    names = fact.get("drug_names") if isinstance(fact, dict) else None
    if not isinstance(names, list):
        return output  # Existing structural checks handle absent/invalid facts.
    for index, name in enumerate(names):
        if not isinstance(name, str) or not name.strip():
            continue
        if _drug_locations(packet, name):
            continue
        entities = [
            e
            for e in packet["entities"]
            if isinstance(e.get("text"), str)
            and e["text"].casefold() == name.casefold()
        ]
        issue = _identity_issue(entities)
        source_names = list(dict.fromkeys(e["text"] for e in entities))
        sources = [(text, _drug_locations(packet, text)) for text in source_names]
        sources = [(text, locations) for text, locations in sources if locations]
        if issue or not sources:
            output["unresolved"].append(
                {
                    "index": index,
                    "model_name": name,
                    "reason": issue or "no_literal_source_entity",
                    "source_ids": [e["id"] for e in entities],
                }
            )
            continue
        source_name, locations = sources[0]
        names[index] = source_name
        output["changes"].append(
            {
                "index": index,
                "model_name": name,
                "source_name": source_name,
                "source_ids": [e["id"] for e in entities if e["text"] == source_name],
                "locations": locations,
                "method": "case_only_source_alignment",
            }
        )
    return output
