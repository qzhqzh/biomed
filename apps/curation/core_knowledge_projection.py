"""Post-hoc typed view of selected source terms; no semantic drug inference."""

import re
from copy import deepcopy

from apps.curation.core_knowledge_intake import _drug_locations

VERSION = "core-knowledge-projection-v1"


def _kind(entities):
    if any(
        reason.get("code") == "ingredient_class_ambiguity"
        for entity in entities
        for guard in entity.get("candidate_guards", {}).values()
        for reason in guard.get("reasons", [])
    ):
        return "drug_class"
    strong_link = any(
        str((e.get("link") or {}).get("candidate_id", "")).startswith(
            ("RXNORM:", "CHEMBL:")
        )
        for e in entities
    )
    types = {e.get("type") for e in entities}
    if "treatment" in types and not strong_link:
        return "regimen"
    if "drug" in types or strong_link:
        return "drug"
    return "other" if entities else "unresolved"


def project(candidate, packet):
    """Return a separate view, leaving raw facts and frozen entities untouched."""
    result = {
        key: []
        for key in (
            "drugs",
            "regimen_mentions",
            "other_source_terms",
            "unresolved_terms",
            "changes",
        )
    }
    by_name = {}
    for entity in packet["entities"]:
        by_name.setdefault(entity["text"], []).append(entity)

    def term(name):
        entities = by_name.get(name, [])
        return {
            "name": name,
            "locations": _drug_locations(packet, name),
            "source_entity_ids": [e["id"] for e in entities],
            "standard_candidates": [
                {"source_entity_id": e["id"], **deepcopy(e["link"])}
                for e in entities
                if (e.get("link") or {}).get("candidate_id")
            ],
            "candidate_guards": {
                e["id"]: deepcopy(e["candidate_guards"])
                for e in entities
                if e.get("candidate_guards")
            },
        }

    def add_drug(name, origin=None):
        drug = next((d for d in result["drugs"] if d["name"] == name), None)
        if drug is None:
            drug = {**term(name), "derived_from": [], "selected_directly": False}
            result["drugs"].append(drug)
        if origin is None:
            drug["selected_directly"] = True
        elif origin not in drug["derived_from"]:
            drug["derived_from"].append(origin)

    names = (candidate.get("fact") or {}).get("drug_names", [])
    for name in dict.fromkeys(names):
        item = term(name)
        kind = _kind(by_name.get(name, [])) if item["locations"] else "unlocated"
        components, classes = [], []
        if kind in {"regimen", "unresolved"} and item["locations"]:
            for text, entities in by_name.items():
                if text == name or not re.search(
                    rf"(?<!\w){re.escape(text)}(?!\w)", name
                ):
                    continue
                component_kind = _kind(entities)
                if component_kind == "drug":
                    add_drug(text, name)
                    components.append(text)
                elif component_kind == "drug_class":
                    classes.append(
                        {
                            **term(text),
                            "kind": "drug_class",
                            "status": "unspecified_member",
                        }
                    )
        if kind == "drug":
            add_drug(name)
            target = "drugs"
        elif kind == "regimen" or components or classes:
            target = "regimen_mentions"
            result[target].append(
                {
                    **item,
                    "components": components,
                    "unresolved_components": classes,
                    "relationship": "not_inferred",
                }
            )
        elif kind == "other":
            target = "other_source_terms"
            result[target].append(
                {**item, "types": sorted({e["type"] for e in by_name[name]})}
            )
        else:
            target = "unresolved_terms"
            result[target].append(
                {
                    **item,
                    "kind": kind,
                    "status": "unspecified_member"
                    if kind == "drug_class"
                    else "unresolved",
                }
            )
        result["changes"].append(
            {"name": name, "target": target, "derived_components": components}
        )
    return result
