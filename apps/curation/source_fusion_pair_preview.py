"""Conservative, inactive preview of an explicitly reported ordered fusion.

This research helper is not connected to the resident workflow. A source span
and a valid target shape do not establish clinical or semantic correctness.
"""

import copy
import re

from apps.curation.knowledge_export import _description, validate_target_payload
from apps.curation.source_linked_facts import evidence_index

VERSION = "biomed.source-fusion-pair-preview.v1"
PAIR = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,14})(?:::|[-–—])([A-Z][A-Z0-9]{1,14})\s+fusions?\b")
NEGATED = re.compile(r"\b(?:no|not|without|negative|absent|suspected|possible)\b", re.IGNORECASE)
CASE = re.compile(r"\b(?:our|the|this)\s+(?:case|patient)\b", re.IGNORECASE)


def _deferred(sample, reason):
    return {"version": VERSION, "group_id": sample.get("group_id"), "status": "deferred", "reason": reason,
            "target_payload": None, "automatic_clinical_matching": False, "target_written": False}


def _local_source(index, aliases, text, *, require_case=False):
    for alias in aliases:
        item = index.get(alias)
        if not item:
            continue
        quote = item["quote"]
        for hit in re.finditer(re.escape(text), quote, re.IGNORECASE):
            left = quote[max(0, hit.start() - 80):hit.start()]
            right = quote[hit.end():hit.end() + 45]
            if NEGATED.search(left) or re.match(r"\s+(?:was\s+|is\s+)?(?:not|negative|absent)\b", right, re.IGNORECASE):
                continue
            if require_case and not CASE.search(quote[:hit.start()]):
                continue
            return {"alias": alias, **copy.deepcopy(item)}
    return None


def preview(sample, *, dictionary, gene_standard):
    """Create an inactive source-bound target shape for a narrowly specified case."""
    record = sample.get("record") or {}
    fact = record.get("fact") or {}
    document = sample.get("source_document") or {}
    metadata = document.get("metadata") or {}
    title = str(record.get("article", {}).get("title") or "")
    raw_types = metadata.get("publication_types")
    if not isinstance(raw_types, list) or metadata.get("title") != title:
        return _deferred(sample, "source_metadata_unresolved")
    types = {str(value).casefold() for value in raw_types}
    if (fact.get("relation") != "treatment_outcome" or "case" not in str(fact.get("scope") or "").casefold()
            or not re.search(r"\bcase\s+report\b", title, re.IGNORECASE) or "review" in types
            or re.search(r"\breview\b", title, re.IGNORECASE)
            or re.search(r"\b(?:two|three|four|[2-9])\s+(?:cases|patients)\b", title, re.IGNORECASE)):
        return _deferred(sample, "not_an_unambiguous_primary_single_case")
    try:
        index = evidence_index(record, document)
    except (ValueError, TypeError, KeyError):
        return _deferred(sample, "source_lineage_invalid")
    aliases = fact.get("evidence")
    if not isinstance(aliases, list) or not aliases or any(not isinstance(value, str) or value not in index for value in aliases):
        return _deferred(sample, "fact_evidence_unresolved")
    entities = {e.get("ref"): e for e in record.get("entities", []) if isinstance(e, dict) and e.get("ref")}
    roles = fact.get("entity_roles") or {}
    if not isinstance(roles, dict):
        return _deferred(sample, "participant_roles_unresolved")
    molecular = [entities[ref] for ref, values in roles.items() if ref in entities and isinstance(values, list)
                 and "molecular" in values]
    pairs = {(hit.group(1), hit.group(2)) for entity in molecular
             if (hit := PAIR.fullmatch(str(entity.get("label") or "").strip()))}
    if len(pairs) != 1:
        return _deferred(sample, "exact_single_fusion_pair_not_established")
    left, right = next(iter(pairs))
    if left == right or left not in gene_standard.get("symbols", {}) or right not in gene_standard.get("symbols", {}):
        return _deferred(sample, "fusion_partners_not_unique_official_genes")
    pair_refs = [e for e in molecular if PAIR.fullmatch(str(e.get("label") or "").strip())]
    allowed_labels = {left, right, *(str(e["label"]) for e in pair_refs)}
    if any(str(e.get("label") or "") not in allowed_labels for e in molecular):
        return _deferred(sample, "additional_molecular_condition_not_resolved")
    population = str(fact.get("population") or "")
    pair_in_population = PAIR.search(population)
    if not pair_in_population or (pair_in_population.group(1), pair_in_population.group(2)) != (left, right):
        return _deferred(sample, "population_does_not_preserve_same_ordered_pair")
    pair_text = pair_refs[0]["label"]
    fusion_source = _local_source(index, aliases, pair_text, require_case=True)
    if not fusion_source:
        return _deferred(sample, "case_fusion_not_explicitly_source_bound")
    drug_refs = [entities[ref] for ref, values in roles.items() if ref in entities and isinstance(values, list)
                 and "intervention" in values and entities[ref].get("kind") == "drug"]
    disease_refs = [entities[ref] for ref, values in roles.items() if ref in entities and isinstance(values, list)
                    and "disease" in values and entities[ref].get("kind") == "disease"]
    if len(drug_refs) != 1 or len(disease_refs) != 1:
        return _deferred(sample, "one_drug_and_one_disease_not_established")
    drug, disease = drug_refs[0], disease_refs[0]
    if (str(fact.get("treatment") or "").strip().casefold() != str(drug.get("label") or "").strip().casefold()
            or fact.get("drug_names") != [fact.get("treatment")]):
        return _deferred(sample, "single_current_drug_not_established")
    results = fact.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return _deferred(sample, "single_result_not_established")
    result = results[0]
    result_alias, value = result.get("passage"), result.get("value")
    if result_alias not in aliases or not isinstance(value, str) or not value.strip():
        return _deferred(sample, "result_provenance_unresolved")
    result_source = _local_source(index, [result_alias], value)
    if (not result_source or not CASE.search(result_source["quote"])
            or not re.search(rf"(?<!\w){re.escape(drug['label'])}(?!\w)", result_source["quote"], re.IGNORECASE)):
        return _deferred(sample, "same_case_treatment_result_not_explicit")
    mapping = sample.get("original_mapping") or {}
    ids = mapping.get("entity_ids") or {}
    drug_id, tumor_id = ids.get(drug["ref"]), ids.get(disease["ref"])
    if not drug_id or not tumor_id:
        return _deferred(sample, "target_drug_or_cancer_id_not_in_frozen_mapping")
    article = record.get("article") or {}
    source = "; ".join(f"{key.upper()}:{article[key]}" for key in ("pmid", "doi") if article.get(key))
    source += f"; biomed:source-fusion-preview/{record['group_id']}"
    description = _description(fact) + "\nOrdered fusion: " + left + "::" + right
    description += "\nSource passage for fusion: " + fusion_source["quote"]
    payload = {"evidence_description": description, "evidence_description_en": description,
               "evidence_category": "treatment", "evidence_source": source, "evidence_level": "-",
               "evidence_type": "", "is_active": False, "comment": f"biomed:source-fusion-preview/{record['group_id']}",
               "conclusion_inputs": [{"potency": "-", "drug_ids": [drug_id],
                   "disease_scope_inputs": [{"tumor_id": tumor_id}],
                   "trigger_inputs": [{"trigger_type": "direct", "event_type": "fusion", "gene_symbol": right,
                                       "target_key": f"{left}::{right}"}]}]}
    validation = validate_target_payload(payload, dictionary)
    if not validation["valid"] or validation.get("unresolved"):
        return {**_deferred(sample, "target_contract_or_ids_unresolved"), "contract_validation": validation}
    return {"version": VERSION, "group_id": sample.get("group_id"), "status": "inactive_structure_preview",
            "reason": "Source-bound candidate; semantic and live target matching require independent validation.",
            "target_payload": payload, "contract_validation": validation,
            "source": {"pmid": article.get("pmid"), "version_id": record.get("version_id"),
                       "source_sha256": record.get("source_sha256"), "fusion": fusion_source, "result": result_source},
            "automatic_clinical_matching": False, "target_written": False, "submit_ready": False,
            "limitations": ["Partner order retained; no generic fusion trigger.",
                            "Study details and assay conditions are retained as text; target does not enforce them.",
                            "Ungraded target treatment reports require separate handling."]}
