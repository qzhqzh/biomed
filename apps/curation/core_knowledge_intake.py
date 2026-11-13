"""Mechanical core-intake projection, preserving drug names and original facts.

This module neither verifies medical meaning nor publishes knowledge. A valid
source location does not establish that a drug participated in the stated fact.
"""

import re
from copy import deepcopy

from scripts.relation_fact_pilot import assemble_one
from scripts.relation_format_diagnostic import adapt

VERSION = "core-knowledge-intake-assembly-v2"
BREAST_GROUP = re.compile(r"\bbreast\s+(?:cancer|carcinoma)\b", re.IGNORECASE)


def _drug_locations(packet, name):
    # A prefix inside a longer word is not a literal mention of a shortened
    # drug name (e.g. "nivo" inside "nivolumab"). No alias expansion is done.
    pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    return [
        {
            "passage_id": passage["id"],
            "role": passage["role"],
            "start": match.start(),
            "end": match.end(),
        }
        for passage in packet["passages"]
        for match in pattern.finditer(passage["text"])
    ]


def _disease_links(entities, packet, fact):
    from_context = False
    population = fact.get("population")
    if (
        not any(e.get("type") == "disease" for e in entities)
        and isinstance(population, str)
        and BREAST_GROUP.search(population)
    ):
        entities = packet["entities"]
        from_context = True
    mentions = [
        {
            "entity_id": entity["id"],
            "original_text": entity["text"],
            "locations": deepcopy(entity["locations"]),
            **({"from_population_context": True} if from_context else {}),
        }
        for entity in entities
        if entity.get("type") == "disease"
        and BREAST_GROUP.search(entity.get("text", ""))
    ]
    return (
        [
            {
                "label": "乳腺癌",
                "policy": "user-approved-breast-group-v1",
                "source_mentions": mentions,
            }
        ]
        if mentions
        else []
    )


def assemble(packet, result):
    """Attach exact sources; preserve raw input and never fill missing drugs."""
    payload, changes = adapt(result.get("payload"))
    view = deepcopy(payload)
    fact = payload.get("fact") if isinstance(payload, dict) else None
    names = fact.get("drug_names") if isinstance(fact, dict) else None
    if isinstance(view, dict) and isinstance(view.get("fact"), dict):
        view["fact"].pop("drug_names", None)
    legacy = assemble_one(packet, {**result, "payload": view})
    diagnostics = [
        issue for issue in legacy["issues"] if issue["code"] == "anchor_not_cited"
    ]
    issues = [
        issue for issue in legacy["issues"] if issue["code"] != "anchor_not_cited"
    ]
    raw_status = result.get("status")
    if raw_status != "classified":
        issues.append({"code": "raw_output_not_classified", "path": "status"})

    valid_names = isinstance(names, list) and all(
        isinstance(name, str) and bool(name.strip()) for name in names
    )
    if isinstance(fact, dict) and not valid_names:
        issues.append({"code": "drug_names_text_list", "path": "fact.drug_names"})

    drugs = []
    if isinstance(fact, dict) and valid_names:
        for name in dict.fromkeys(names):
            locations = _drug_locations(packet, name)
            if not locations:
                issues.append(
                    {"code": "drug_name_not_literal", "path": f"fact.drug_names:{name}"}
                )
            matching = [
                entity for entity in packet["entities"] if entity.get("text") == name
            ]
            standards = [
                {"source_entity_id": entity["id"], **deepcopy(entity["link"])}
                for entity in matching
                if isinstance(entity.get("link"), dict)
                and entity["link"].get("candidate_id")
            ]
            drugs.append(
                {
                    "name": name,
                    "locations": locations,
                    "source_entity_ids": [entity["id"] for entity in matching],
                    "standard_candidates": standards,
                }
            )

    candidate = deepcopy(legacy["candidate"])
    if candidate is not None:
        candidate["fact"] = deepcopy(fact)
        candidate["drugs"] = drugs
        candidate["business_disease_links"] = _disease_links(
            candidate["entities"], packet, fact
        )
        if not candidate["evidence"]:
            issues.append({"code": "no_actual_evidence", "path": "fact.evidence"})
    if valid_names and drugs and all(drug["locations"] for drug in drugs):
        diagnostics.extend(e for e in issues if e["code"] == "empty_entity_ids")
        issues = [e for e in issues if e["code"] != "empty_entity_ids"]
    anchor_cited = bool(
        candidate and any(e["role"] == "anchor" for e in candidate["evidence"])
    )
    if legacy["status"] == "abstained" and not issues:
        status = "abstained"
    elif candidate is not None and not issues:
        status = "core_structurally_valid"
    else:
        status = "invalid_structure"
    return {
        "version": VERSION,
        "raw_status": raw_status,
        "format_changes": changes,
        "structure_status": status,
        "issues": issues,
        "diagnostics": diagnostics,
        "anchor_cited": anchor_cited,
        "candidate": candidate,
    }
