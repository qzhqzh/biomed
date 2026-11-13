"""Source-preserving extraction assembly, without semantic review or publication."""

import re
import time
from copy import deepcopy

from apps.curation.compound_entity_mentions import expand_inventory
from apps.curation.decision import digest
from apps.curation.knowledge_eligibility import assess_record
from apps.curation.source_entity_types import refine
from apps.curation.source_relation_types import refine_relation

VERSION = "minimal-knowledge-assembly-v6"
KINDS = {"disease", "drug", "gene", "protein", "variant", "biomarker", "treatment"}
ROLES = {"disease", "intervention", "comparator", "molecular", "context"}
RELATIONS = {"treatment_outcome", "biomarker_association", "mechanism", "recommendation", "other"}


def occurrences(text, mention, kind):
    # Preserve source spelling; case-insensitive recovery is limited to drug names.
    flags = re.IGNORECASE if kind == "drug" else 0
    return list(re.finditer(rf"(?<!\w){re.escape(mention)}(?!\w)", text, flags))


def molecular_slot_match(text, entity):
    """HGVS typography may differ in prose; source mentions/offsets stay exact."""
    label = entity["text"]
    if entity["type"] == "gene":
        return bool(re.search(rf"(?<!\w){re.escape(label)}(?=p\.[A-Z]|\b)", text))
    match = re.fullmatch(r"([A-Z][A-Z0-9-]{1,19})\s*(?:p\.)?([ACDEFGHIKLMNPQRSTVWY][1-9]\d{0,5}[ACDEFGHIKLMNPQRSTVWY*])", label)
    if entity["type"] == "variant" and match:
        return bool(re.search(rf"(?<!\w){re.escape(match[1])}\s*(?:p\.)?{re.escape(match[2])}(?!\w)", text))
    return False


def inventory_roles(entity, fact, packet=None):
    """Conservative slot membership, never all model-selected inventory IDs."""
    roles = []
    fields = {"disease": ["population"], "intervention": ["treatment"],
              "comparator": ["comparator"], "molecular": ["conditions", "population", "results", "conclusion"]}
    kind = entity["type"]
    names = [entity["text"]]
    if packet and re.fullmatch(r"[A-Z]{2,10}", entity["text"]):
        from scripts.light_entity_pilot import local_aliases
        # Only explicit same-source definitions with exact initials, not arbitrary
        # suffixes returned by the broader dictionary candidate-retrieval helper.
        names += [alias for alias in local_aliases(packet, entity["text"])
                  if "".join(word[0].upper() for word in re.findall(r"[A-Za-z]+", alias)) == entity["text"]]
    for role, keys in fields.items():
        eligible = ((role == "disease" and kind == "disease")
                    or (role in {"intervention", "comparator"} and kind in {"drug", "treatment"})
                    or (role == "molecular" and kind in {"gene", "protein", "variant", "biomarker"}))
        if not eligible:
            continue
        for key in keys:
            value = fact.get(key)
            # Formatting existing slot text does not infer an identity or relation.
            if (any(occurrences(str(value or ""), name, kind) for name in names)
                    or (role == "molecular" and molecular_slot_match(str(value or ""), entity))):
                roles.append(role)
                break
    return roles or ["context"]


def normalize(packet, result, *, mode, article_context=(), linker=None):
    """Keep failed/unlocated claims visible; only exact mentions become anchors."""
    payload = result.get("payload")
    fact = payload.get("fact") if isinstance(payload, dict) else None
    issues, deferred, raw_entities = [], [], []
    if not isinstance(fact, dict):
        return {"fact": {}, "entities": [], "anchors": [], "issues": ["no_extracted_fact"],
                "deferred": [{"reason": (payload or {}).get("reason") if isinstance(payload, dict) else result.get("status")}],
                "execution_status": "failed", "type_changes": [], "fact_changes": []}
    fact = deepcopy(fact)
    relation_view = refine_relation(packet, fact)
    fact = relation_view["fact"]
    if result.get("status") != "classified":
        issues.append("model_output_not_complete")
    if fact.get("relation") not in RELATIONS:
        issues.append("invalid_relation")
    for field in ("population", "treatment", "comparator", "scope"):
        if not (fact.get(field) is None or isinstance(fact.get(field), str)):
            issues.append(f"invalid_{field}")
    if not isinstance(fact.get("conditions"), list) or not all(isinstance(x, str) for x in fact.get("conditions", [])):
        issues.append("invalid_conditions")
    evidence = fact.get("evidence", [])
    aliases = {p["alias"]: p for p in packet["passages"]}
    if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or x not in aliases for x in evidence):
        issues.append("invalid_evidence")
        evidence = [x for x in evidence if isinstance(x, str) and x in aliases] if isinstance(evidence, list) else []
    if not any(aliases[a]["role"] == "anchor" for a in evidence):
        issues.append("target_anchor_not_cited")
    if mode == "inventory":
        inventory = expand_inventory(packet)
        selected_diseases = [e["id"] for e in inventory
                             if e["type"] == "disease" and not e.get("compound_context_only")
                             and (e["id"] in fact.get("entity_ids", [])
                                  or e.get("derived_from", {}).get("ref") in fact.get("entity_ids", []))]
        for item in inventory:
            roles = inventory_roles(item, fact, packet)
            if item.get("compound_context_only"):
                roles = ["context"]
            # entity_ids is the relation model's explicit participant selection.
            # A sole selected disease supplies the disease role even when the
            # population slot uses its full name or an experimental model name.
            # Unselected inventory diseases remain context, never automatically joined.
            selected_disease = selected_diseases == [item["id"]]
            if selected_disease:
                roles = ["disease"]
            raw_entities.append({**deepcopy(item), "roles": roles,
                                 "role_basis": "unique_model_selected_disease" if selected_disease else "literal_fact_slot_or_explicit_source_definition"})
    else:
        entities = fact.pop("entities", None)
        if not isinstance(entities, list) or not entities:
            issues.append("missing_entities")
            entities = []
        if not isinstance(fact.get("conclusion"), str) or not fact["conclusion"].strip():
            issues.append("missing_conclusion")
        for i, item in enumerate(entities):
            if not isinstance(item, dict) or item.get("type") not in KINDS or not isinstance(item.get("text"), str) or not item["text"].strip():
                deferred.append({"index": i, "reason": "invalid_entity", "raw": item})
                issues.append("invalid_entity")
                continue
            role_list = item.get("roles")
            if not isinstance(role_list, list) or not role_list or any(role not in ROLES for role in role_list):
                issues.append("invalid_entity_roles")
                role_list = ["context"]
            passage = aliases.get(item.get("passage"))
            locs = []
            name = item["text"]
            search = [passage] if passage and occurrences(passage["text"], name, item["type"]) else packet["passages"]
            for actual_passage in search:
                matches = occurrences(actual_passage["text"], name, item["type"])
                for match in matches:
                    # Only identical source spellings share a single source mention.
                    actual = match.group()
                    if locs and actual != name:
                        continue
                    name = actual
                    locs.append({"passage_id": actual_passage["id"], "role": actual_passage["role"],
                                 "start": match.start(), "end": match.end()})
            raw_entities.append({"id": f"{packet['id']}.e{i + 1}", "type": item["type"],
                                 "text": name, "raw_text": item["text"], "locations": locs,
                                 "roles": sorted(set(role_list)), "link": None,
                                 "model_passage": item.get("passage"), "role_basis": "source_only_model"})
    # Validate original offsets in both arms; never manufacture missing mentions.
    by_id = {p["id"]: p for p in packet["passages"]}
    entries, seen = [], {}
    for entity in raw_entities:
        kind, text = entity.get("type"), entity.get("text")
        if kind not in KINDS or not isinstance(text, str):
            deferred.append({"ref": entity.get("id"), "reason": "invalid_entity"})
            issues.append("invalid_entity")
            continue
        locs = []
        for loc in entity.get("locations", []):
            p = by_id.get(loc.get("passage_id"))
            start, end = loc.get("start"), loc.get("end")
            if (p and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(p["text"])
                    and p["text"][start:end] == text
                    and any(m.start() == start and m.end() == end for m in occurrences(p["text"], text, kind))):
                locs.append(loc)
        if not locs:
            deferred.append({"ref": entity["id"], "text": text, "roles": entity["roles"], "reason": "unlocated_entity"})
            if entity["roles"] != ["context"]:
                issues.append("unlocated_participant")
            continue
        key = (kind, text)
        if key in seen:
            seen[key]["roles"] = sorted(set(seen[key]["roles"] + entity["roles"]))
            for loc in locs:
                if loc not in seen[key]["locations"]:
                    seen[key]["locations"].append(loc)
            continue
        entity = {**entity, "locations": locs}
        # Both arms use the same dictionary/guard path, not preselected AI identities.
        if linker:
            entity.update(linker(entity, packet))
        else:
            entity["link"] = {"candidate_id": None, "status": "not_linked"}
        # Reuse the existing class guard as a typed projection. Keep its evidence;
        # do not turn a class into an invented concrete ingredient.
        if kind == "drug" and any(reason.get("code") == "ingredient_class_ambiguity"
                for guard in entity.get("candidate_guards", {}).values()
                for reason in guard.get("reasons", [])):
            entity.update(type="treatment", subtype="drug_class",
                          type_refinement={"original_type": kind, "rule": "existing_ingredient_class_guard"})
        qualifiers = []
        if kind in {"drug", "treatment"}:
            mention = re.escape(text)
            negative = re.compile(
                rf"\b(?:without|no)\s+(?:(?:concurrent|concomitant|additional|adjuvant|neoadjuvant)\s+){{0,2}}{mention}(?!\w)"
                rf"|(?<!\w){mention}(?!\w)\s+(?:(?:chemotherapy|therapy|treatment)\s+)?"
                r"(?:(?:was|were|is|has been|had been)\s+)?(?:not administered|not used|withheld)\b", re.IGNORECASE)
            prior = re.compile(rf"\b(?:prior|previous)\s+(?:(?:exposure to|treatment with|therapy with)\s+)?{mention}(?!\w)", re.IGNORECASE)
            if "intervention" in entity["roles"] and negative.search(str(fact.get("treatment") or "")):
                entity["roles"] = [r for r in entity["roles"] if r != "intervention"] or ["context"]
                qualifiers.append("not_administered_in_treatment_slot")
            if "comparator" in entity["roles"] and prior.search(str(fact.get("comparator") or "")):
                qualifiers.append("prior_exposure_comparator")
        entity["role_qualifiers"] = qualifiers
        entries.append(entity)
        seen[key] = entity
    typed = refine(packet, entries, article_context or packet["passages"])
    retyped_genes = {change["entity_id"]: change for change in typed["changes"] if change["to_type"] == "gene"}
    entities, anchors, role_map = [], [], {}
    for entity in typed["entities"]:
        if entity["id"] in retyped_genes:
            previous_roles = entity["roles"]
            entity["roles"] = inventory_roles(entity, fact, packet)
            entity["role_basis"] = "source_gene_type_then_literal_fact_slot"
            retyped_genes[entity["id"]]["role_projection"] = {"before": previous_roles, "after": entity["roles"],
                "method": "literal_fact_slot_after_source_type_change"}
        roles, ref = entity["roles"], entity["id"]
        role_map[ref] = roles
        entities.append({"ref": ref, "kind": entity["type"], "subtype": entity.get("subtype", ""),
                         "label": entity["text"], "attributes": {"identity_status": "source_scoped_candidate"}})
        for loc in entity["locations"]:
            p = by_id[loc["passage_id"]]
            anchors.append({"entity_ref": ref, "passage_id": p["id"],
                            "parser_version": p["parser_version"], "locator": p["locator"],
                            "start": loc["start"], "end": loc["end"], "quote": entity["text"],
                            "purpose": "mention", "roles": roles,
                            "association": "context_only" if roles == ["context"] else "exact_fact_text" if mode == "inventory" else "model_selected",
                            "attributes": {"standard_link": entity.get("link") or {},
                                           "role_basis": entity["role_basis"],
                                           "model_passage": entity.get("model_passage"),
                                           "role_qualifiers": entity.get("role_qualifiers", []),
                                           "derived_from": entity.get("derived_from"),
                                           "original_type": entity.get("type_refinement", {}).get("original_type", entity["type"])}})
    for p in packet["passages"]:
        anchors.append({"entity_ref": None, "passage_id": p["id"],
                        "parser_version": p["parser_version"], "locator": p["locator"],
                        "start": 0, "end": len(p["text"]), "quote": p["text"],
                        "purpose": "evidence" if p["alias"] in evidence else "context",
                        "association": "context_only", "roles": [],
                        "attributes": {"alias": p["alias"], "input_role": p["role"],
                                       **({"ancestor_tags": p["ancestor_tags"]} if "ancestor_tags" in p else {})}})
    fact["entity_roles"] = role_map
    fact["entity_ids"] = [ref for ref, roles in role_map.items() if roles != ["context"]]
    if mode == "direct":
        fact["drug_names"] = [e["label"] for e in entities if e["kind"] == "drug" and "intervention" in role_map[e["ref"]]]
    if not fact["entity_ids"]:
        issues.append("no_located_participants")
    return {"fact": fact, "entities": entities, "anchors": anchors, "issues": sorted(set(issues)),
            "deferred": deferred, "type_changes": typed["changes"], "fact_changes": relation_view["changes"],
            "execution_status": "partial" if issues else "complete"}


def build_bundle(packets, results, *, source_run, mode="direct", linker=None):
    """Assemble bounded independent facts; legacy one-fact outputs still work."""
    expanded_packets, expanded_results, origins = [], {}, {}
    for packet in packets:
        result = results[packet["id"]]
        payload = result.get("payload")
        multiple = isinstance(payload, dict) and "facts" in payload
        facts = payload.get("facts") if multiple else None
        if multiple and (not isinstance(facts, list) or len(facts) > 3 or any(not isinstance(f, dict) for f in facts)):
            result = {**result, "status": "invalid_fact_array", "payload": None}
            facts = None
        for index, fact in enumerate(facts or [None]):
            group_id = packet["id"] if index == 0 else f"{packet['id']}.f{index + 1:02d}"
            expanded_packets.append({**packet, "id": group_id})
            expanded_results[group_id] = ({**result, "payload": {"fact": fact, "reason": payload.get("reason")}}
                                          if multiple and facts is not None else result)
            origins[group_id] = {"source_packet_id": packet["id"], "fact_index": index,
                                 "result_sha256": digest(results[packet["id"]])}
    bundle, report = _build_single_facts(expanded_packets, expanded_results, source_run=source_run, mode=mode, linker=linker)
    for record in bundle["records"]:
        record["provenance"].update(origins[record["group_id"]])
    report["source_packets"] = len(packets)
    return bundle, report


def _build_single_facts(packets, results, *, source_run, mode="direct", linker=None):
    start = time.perf_counter()
    context = {p["id"]: p for packet in packets for p in packet["passages"]}
    records, issues = [], []
    for packet in packets:
        result = results[packet["id"]]
        view = normalize(packet, result, mode=mode, article_context=list(context.values()), linker=linker)
        if view["issues"]:
            issues.append({"id": packet["id"], "issues": view["issues"], "deferred": view["deferred"]})
        if not view["anchors"]:
            # Save failed attempt with original context, not a fabricated fact.
            for p in packet["passages"]:
                view["anchors"].append({"entity_ref": None, "passage_id": p["id"],
                    "parser_version": p["parser_version"], "locator": p["locator"],
                    "start": 0, "end": len(p["text"]), "quote": p["text"],
                    "purpose": "context", "association": "context_only", "roles": [], "attributes": {"alias": p["alias"]}})
        records.append({"group_id": packet["id"], "version_id": packet["article"]["version_id"],
                        "source_sha256": packet["article"]["source_sha256"], "article": packet["article"],
                        "fact": view["fact"], "entities": view["entities"], "anchors": view["anchors"],
                        "assessment": {"execution": {"status": view["execution_status"], "issues": view["issues"]},
                                       "compatibility": {"status": "unassessed"}, "quality": {"status": "unreviewed"}},
                        "provenance": {"version": VERSION, "mode": mode, "result_sha256": digest(result),
                                       "input_sha256": result.get("input_sha256"), "raw_fact": (result.get("payload") or {}).get("fact"),
                                       "unresolved_entities": view["deferred"], "type_changes": view["type_changes"],
                                       "fact_changes": view["fact_changes"]}})
        records[-1]["assessment"]["eligibility"] = assess_record(records[-1], passages=packet["passages"])
    return ({"schema_version": "knowledge-candidates-v1", "source_run": source_run,
             "provenance": {"version": VERSION, "mode": mode, "semantic_review_is_independent": True}, "records": records},
            {"version": VERSION, "mode": mode, "records": len(records), "issues": issues,
             "structurally_eligible_records": sum(r["assessment"]["eligibility"]["status"] == "eligible" for r in records),
             "deferred_knowledge": [{"id": r["group_id"], **r["assessment"]["eligibility"]} for r in records
                                    if r["assessment"]["eligibility"]["status"] == "deferred"],
             "script_seconds": round(time.perf_counter() - start, 6)})
