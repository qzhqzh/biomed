"""Opt-in source-linked target preview; does not alter the resident pipeline.

This is a conservative experiment, not a clinical validation or an importer.
The source links prove where text came from, not that the interpretation is
correct. Results remain inactive and the original candidate is never changed.
"""

import copy
import re

from apps.curation.knowledge_eligibility import assess_record
from apps.curation.knowledge_export import _roles, export_record
from apps.curation.knowledge_target_mapping import (
    VARIANT,
    _additional_result_molecular_scope,
    _single_drug_phrase,
    _variant_candidates,
)
from apps.curation.source_disease_projection import project_main_disease
from apps.curation.source_linked_facts import evidence_index, normalize

VERSION = "biomed.source-linked-target-preview.v1"


def _contains(text, term):
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", str(text or ""), re.IGNORECASE))


def _common_evidence(fact, gene, variant, drug, entities):
    """Require shared, exact spans and reject a few explicit conflicting scopes.

    Even a shared paragraph can refer to different subjects. A surviving
    result therefore stays a semantic candidate and is not automatically used.
    """
    molecular = {item["passage_id"] for item in fact["molecular_event"]["evidence"]}
    intervention = {item["passage_id"] for item in fact["intervention"]["evidence"]}
    disease = {item["passage_id"] for item in fact["disease"]["evidence"]}
    common = molecular & intervention & disease
    selected = []
    for item in fact["cohort_evidence"]:
        quote = item["quote"]
        if item["passage_id"] not in common or not all(_contains(quote, x) for x in (gene, variant, drug)):
            continue
        if {m.group(1).upper() for m in VARIANT.finditer(quote)} - {variant}:
            continue
        if re.search(r"\b(?:PD[- ]?L1|TMB|MSI|VAF|ctDNA|co[- ]occurring)\b", quote, re.IGNORECASE):
            continue
        if re.search(r"\b(?:no|not|without|negative for|wild[- ]type|absence of)\b.{0,40}"
                     + re.escape(gene), quote, re.IGNORECASE):
            continue
        if re.search(re.escape(variant) + r".{0,25}\b(?:not detected|negative|absent)\b", quote, re.IGNORECASE):
            continue
        if re.search(r"\b(?:other|another|different|control|untreated)\s+(?:patients?|cohort|group)\b",
                     quote, re.IGNORECASE):
            continue
        if re.search(r"\b(?:prior|previous(?:ly)?)\s+(?:therapy\s+with\s+)?" + re.escape(drug),
                     quote, re.IGNORECASE):
            continue
        if re.search(re.escape(drug) + r"\s+(?:and|plus|combined with|in combination with)\b|"
                     + re.escape(drug) + r"\s*\+", quote, re.IGNORECASE):
            continue
        other_drugs = [e["label"] for e in entities if e.get("kind") == "drug"
                       and e.get("label", "").casefold() != drug.casefold()
                       and _contains(quote, e.get("label"))]
        if other_drugs:
            continue
        selected.append({"passage_id": item["passage_id"], "quote": quote})
    return selected


def _result_supported(record, fact, index):
    results = record.get("fact", {}).get("results")
    if not isinstance(results, list) or not results:
        return False
    outcome = fact["outcome"]
    evidence = {item["passage_id"]: item for item in outcome["evidence"]}
    for result in results:
        if not isinstance(result, dict):
            return False
        value, anchor = result.get("value"), index.get(result.get("passage"))
        if (not isinstance(value, str) or not value.strip() or not anchor
                or anchor["passage_id"] not in evidence
                or value.casefold() not in str(outcome.get("text") or "").casefold()
                or value.casefold() not in anchor["quote"].casefold()):
            return False
    return True


def _result(sample, reason, *, linked=None):
    return {"version": VERSION, "group_id": sample["record"]["group_id"],
            "status": "baseline_preserved" if sample["original_export"].get("offline_ready") else "deferred",
            "reason": reason, "original_export": copy.deepcopy(sample["original_export"]),
            "linked_fact": linked, "derived_export": None, "semantic_status": "unreviewed",
            "automatic_clinical_matching": False, "target_contacted": False, "target_written": False}


def preview(sample, payload, *, dictionary, gene_standard, disease_nodes, ontology_source):
    """Create an inactive, provenance-preserving preview, or preserve baseline."""
    if sample["original_export"].get("offline_ready"):
        return _result(sample, "previously_ready_record_is_preserved")
    if not isinstance(payload, dict):
        return _result(sample, "model_output_unavailable_or_invalid_json")
    try:
        linked = normalize(sample, payload)
        index = evidence_index(sample["record"], sample["source_document"])
    except (ValueError, TypeError, KeyError):
        return _result(sample, "source_or_model_shape_invalid")
    if linked["status"] != "source_linked":
        return _result(sample, "source_linked_fact_incomplete", linked=linked)
    record = sample["record"]
    fact = record.get("fact", {})
    molecular, intervention = linked.get("molecular_event"), linked.get("intervention")
    if (fact.get("relation") != "treatment_outcome" or linked["relation"] != "treatment_outcome"
            or linked["source_scope"] != "primary" or linked["same_cohort"] is not True
            or not molecular or not intervention or molecular.get("event_type") != "mutation"
            or molecular.get("status") != "present" or molecular.get("role") != "eligibility"
            or intervention.get("role") != "current"):
        return _result(sample, "target_relation_or_scope_not_established", linked=linked)
    roles = _roles(record)
    entities = {e["ref"]: e for e in record.get("entities", [])}
    molecular_refs = [ref for ref, items in roles.items() if "molecular" in items and ref in entities]
    disease_refs = [ref for ref, items in roles.items() if "disease" in items and ref in entities]
    drug_refs = [ref for ref, items in roles.items() if "intervention" in items and ref in entities]
    drug_ref, disease_ref = intervention.get("entity_ref"), linked["disease"].get("entity_ref")
    candidates = _variant_candidates(record, molecular_refs)
    if (len(candidates) != 1 or len(disease_refs) != 1 or disease_ref != disease_refs[0]
            or len(drug_refs) != 1 or drug_ref != drug_refs[0]
            or set(molecular.get("entity_refs", [])) != set(molecular_refs)):
        return _result(sample, "original_participant_or_event_scope_ambiguous", linked=linked)
    gene, variant, refs = candidates[0]
    if (not isinstance(fact.get("conditions"), list)
            or any(not isinstance(value, str) for value in fact["conditions"])):
        return _result(sample, "original_conditions_unresolved", linked=linked)
    conditions_text = " ".join(fact["conditions"])
    other_event_genes = {word.upper() for word in re.findall(r"(?<!\w)[A-Z][A-Z0-9-]{1,19}(?!\w)",
                                                               str(molecular.get("event") or ""))
                         if word.upper() in gene_standard.get("symbols", {}) and word.upper() != gene}
    additional_condition_genes = {match.group(1).upper() for match in re.finditer(
        r"(?<![\w-])([A-Z][A-Z0-9-]{1,19})\s+(?:mutations?|mutated|mutant|alterations?|altered)\b",
        conditions_text) if match.group(1).upper() in gene_standard.get("symbols", {}) and match.group(1).upper() != gene}
    if (set(refs) != set(molecular_refs) or gene != str(molecular.get("gene") or "").upper()
            or not _contains(molecular.get("event"), variant) or gene not in gene_standard.get("symbols", {})
            or _additional_result_molecular_scope(fact, gene, variant)
            or bool(other_event_genes or additional_condition_genes)
            or bool({m.group(1).upper() for m in VARIANT.finditer(conditions_text)} - {variant})
            or bool(re.search(r"\b(?:PD[- ]?L1|TMB|MSI|VAF|ctDNA|co[- ]occurring)\b", conditions_text, re.IGNORECASE))
            or str(fact.get("molecular_logic") or "").casefold() not in {"", "single"}
            or not _single_drug_phrase(str(fact.get("treatment") or ""), entities[drug_ref]["label"])):
        return _result(sample, "single_known_event_or_treatment_not_established", linked=linked)
    support = _common_evidence(linked, gene, variant, entities[drug_ref]["label"], list(entities.values()))
    if not support or not _result_supported(record, linked, index):
        return _result(sample, "cohort_or_result_support_insufficient", linked=linked)

    old_execution = copy.deepcopy(record.get("assessment", {}).get("execution", {}))
    issues = old_execution.get("issues") or []
    if old_execution.get("status") != "complete" and not (
            old_execution.get("status") == "partial" and issues == ["target_anchor_not_cited"]):
        return _result(sample, "original_execution_has_other_unresolved_issues", linked=linked)
    if set(issues) - {"target_anchor_not_cited"}:
        return _result(sample, "original_execution_has_other_unresolved_issues", linked=linked)

    mapping = copy.deepcopy(sample["original_mapping"])
    projection = None
    if disease_ref in mapping.get("unresolved_entity_refs", []):
        diagnostic = mapping.get("identity_diagnostics", {}).get(disease_ref, {})
        projection = project_main_disease(entities[disease_ref], index, disease_nodes, dictionary,
                                          prior_status=diagnostic.get("status"), ontology_source=ontology_source)
        if not projection:
            return _result(sample, "main_cancer_identity_unresolved", linked=linked)
        existing_id = mapping.get("entity_ids", {}).get(disease_ref)
        if existing_id and existing_id != projection["target_id"]:
            return _result(sample, "conflicting_existing_target_identity", linked=linked)
        mapping.setdefault("entity_ids", {})[disease_ref] = projection["target_id"]
        mapping["unresolved_entity_refs"].remove(disease_ref)
        mapping.setdefault("identity_diagnostics", {}).pop(disease_ref, None)
        mapping["basis"] = str(mapping.get("basis") or "") + "; source-defined subtype projected to frozen main cancer"

    clinical_context = copy.deepcopy(fact.get("conditions") or [])
    mapping["trigger_scope"] = {
        "kind": "direct", "logic": "single", "condition_entity_refs": list(refs),
        "non_trigger_entity_refs": [], "source_support": support,
        "unstructured_clinical_context": clinical_context, "clinical_context_enforced_by_target": False,
        "reason": "Model assertion with exact shared evidence; clinical semantics remain unreviewed.",
    }
    mapping["trigger_bindings"] = [{
        "entity_refs": list(refs), "basis": "Exact source event with a locally confirmed gene; semantic review pending.",
        "source_support": support, "gene_standard_identity": copy.deepcopy(gene_standard["symbols"][gene]),
        "gene_standard_source": copy.deepcopy(gene_standard.get("source", {})),
        "input": {"trigger_type": "direct", "event_type": "mutation", "gene_symbol": gene, "target_key": variant},
    }]
    mapping["treatment_scope"] = {
        "kind": "single", "entity_refs": [drug_ref], "groups": [{"entity_refs": [drug_ref], "label": fact["treatment"]}],
        "source_support": support, "dose_enforced_by_target": False,
        "reason": "The existing fact and model identify one drug; semantic review remains pending.",
    }
    derived = copy.deepcopy(record)
    if "target_anchor_not_cited" in issues:
        derived.setdefault("assessment", {})["execution"] = {
            "status": "complete", "issues": [], "recovery": "original result linked to cited source; semantic review pending"}
        derived["assessment"]["eligibility"] = assess_record(derived)
    derived.setdefault("provenance", {})[VERSION] = {
        "original_execution": old_execution, "source_linked_extraction": linked,
        "disease_projection": projection, "source_sample_sha256": sample.get("sample_sha256"),
        "semantic_status": "unreviewed", "automatic_clinical_matching": False,
    }
    exported = export_record(derived, source_run=f"source-linked-preview:{sample.get('batch_id', 'unknown')}",
                             dictionary=dictionary, mapping=mapping, gene_standard=gene_standard)
    structure_passed = exported["offline_ready"] and exported["target_readiness"]["status"] == "offline_ready"
    return {"version": VERSION, "group_id": record["group_id"],
            "status": "offline_structure_only" if structure_passed else "deferred",
            "reason": "semantic_validation_and_automatic_matching_not_established" if structure_passed else "target_contract_not_passed",
            "original_export": copy.deepcopy(sample["original_export"]), "linked_fact": linked,
            "derived_export": exported, "disease_projection": projection,
            "semantic_status": "unreviewed", "automatic_clinical_matching": False,
            "target_contacted": False, "target_written": False}
