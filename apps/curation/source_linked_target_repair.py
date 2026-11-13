"""Opt-in repair for source-bound gene, cancer alias and treatment context.

This creates a new inactive preview only. It preserves the old candidate and
explicitly distinguishes a valid target shape from automatic patient matching.
"""

import copy
import re
import uuid

from apps.curation.knowledge_eligibility import assess_record
from apps.curation.knowledge_export import _roles, export_record
from apps.curation.knowledge_target_mapping import VARIANT, _disease_name_key
from apps.curation.source_linked_facts import evidence_index
from apps.curation.source_linked_target_mapping import preview as baseline_preview
from apps.curation.source_study_context import assess as study_context

VERSION = "biomed.source-linked-target-repair.v1"


def _contains(text, value):
    return bool(value and re.search(rf"(?<!\w){re.escape(str(value))}(?!\w)", str(text or ""), re.IGNORECASE))


def _gene_variant_source(document, gene, variant, standard):
    """Accept a direct spelling or an explicit single-gene mutation list."""
    direct = re.compile(rf"(?<!\w){re.escape(gene)}\s*(?:p\.)?{re.escape(variant)}(?!\w)", re.IGNORECASE)
    listed = re.compile(rf"(?<!\w){re.escape(gene)}\s+(?:mutations?|variants?)\s*,?\s*"
                        rf"(?:including|such as)\s+([^.;]{{0,180}}\b{re.escape(variant)}\b)", re.IGNORECASE)
    found = []
    for passage in document.get("passages", []):
        text = passage.get("text")
        if not isinstance(text, str):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            match = direct.search(sentence) or listed.search(sentence)
            if not match:
                continue
            if re.search(r"\b(?:no|not|without|except|negative|wild[- ]type|unrelated)\b", sentence, re.IGNORECASE):
                continue
            # Do not attribute a listed variant when another approved gene is
            # part of the span; locality alone is never enough to pair them.
            part = match.group(0)
            other_genes = {word for word in re.findall(r"(?<!\w)[A-Z][A-Z0-9-]{1,19}(?!\w)", part)
                           if word in standard.get("symbols", {}) and word != gene}
            if other_genes:
                continue
            found.append(passage)
            break
    return found


def _alias_ref(record, mapping, linked_ref, entities):
    """Keep one disease only if full name and abbreviation are source-defined."""
    roles = _roles(record)
    refs = [ref for ref, role in roles.items() if "disease" in role and ref in entities]
    if len(refs) == 1 and refs[0] == linked_ref:
        return []
    if len(refs) != 2 or linked_ref not in refs:
        return None
    ids = [mapping.get("entity_ids", {}).get(ref) for ref in refs]
    try:
        if not ids[0] or uuid.UUID(str(ids[0])) != uuid.UUID(str(ids[1])):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    first, second = (entities[ref].get("label", "") for ref in refs)
    full, abbreviation = sorted((first, second), key=len, reverse=True)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,14}", abbreviation):
        return None
    full_key, abbr_key = _disease_name_key(full), _disease_name_key(abbreviation)
    definition = re.compile(rf"(?<!\w){re.escape(full_key)}\s*\(\s*{re.escape(abbr_key)}\s*\)")
    marker = re.compile(rf"\(\s*{re.escape(abbr_key)}\s*\)")
    found = False
    for anchor in record.get("anchors", []):
        if anchor.get("purpose") != "evidence":
            continue
        quote = _disease_name_key(anchor.get("quote", ""))
        for hit in marker.finditer(quote):
            before = quote[max(0, hit.start() - 150):hit.end()]
            if not definition.search(before) or re.search(
                    r"\b(?:not|no|without|unrelated)\b.{0,40}" + re.escape(full_key), before):
                return None
            found = True
    if not found:
        return None
    return [ref for ref in refs if ref != linked_ref]


def _outcome_source(linked, variant, drug, value, first_line, *, other_drugs=()):
    """Find a source-specific variant, regimen and literal result together."""
    cohort_ids = {item["passage_id"] for item in linked.get("cohort_evidence", [])}
    for item in linked.get("outcome", {}).get("evidence", []):
        text = item.get("quote", "")
        if item.get("passage_id") not in cohort_ids or not all(_contains(text, term) for term in (variant, drug)):
            continue
        if {m.group(1).upper() for m in VARIANT.finditer(text)} != {variant}:
            continue
        if re.search(r"\b(?:other|different|another|control)\s+(?:patients?|cohort|group)\b", text, re.IGNORECASE):
            continue
        if re.search(r"\b(?:no|not|without|negative for|wild[- ]type)\b.{0,40}" + re.escape(variant), text, re.IGNORECASE):
            continue
        if re.search(re.escape(drug) + r"\s+(?:and|plus|combined with|in combination with)\b|"
                     + re.escape(drug) + r"\s*\+", text, re.IGNORECASE):
            continue
        # A regimen must appear before its own numerical result, without a
        # second regimen or sentence intervening. This is a candidate check,
        # not proof that the selected clinical groups are interchangeable.
        pair = re.search(rf"(?<!\w){re.escape(drug)}(?!\w)[^.;]{{0,80}}{re.escape(value)}", text, re.IGNORECASE)
        if not pair:
            continue
        if (re.search(r"\b(?:whereas|versus|vs|while|but|other|another|and|or|plus|combined)\b",
                      pair.group(0), re.IGNORECASE)
                or any(_contains(pair.group(0), other) for other in other_drugs)):
            continue
        if first_line and not re.search(r"\bfirst[- ]line\s+therapy\b.{0,120}"
                                        + re.escape(drug) + r"[^.;]{0,80}" + re.escape(value), text, re.IGNORECASE):
            continue
        return item
    return None


def _add_anchor(record, passage):
    used = {(a.get("attributes") or {}).get("alias") for a in record.get("anchors", [])}
    numbers = [int(alias[1:]) for alias in used if isinstance(alias, str) and re.fullmatch(r"P[1-9][0-9]*", alias)]
    alias = f"P{max(numbers, default=0) + 1}"
    text = passage["text"]
    record["anchors"].append({"entity_ref": None, "passage_id": passage["id"], "parser_version": passage.get("parser_version"),
        "locator": passage["locator"], "start": 0, "end": len(text), "quote": text, "purpose": "evidence",
        "association": "context_only", "roles": [], "attributes": {"alias": alias, "input_role": "support"}})
    return alias


def _change_roles(record, ref, *, add=(), remove=()):
    roles = record["fact"].setdefault("entity_roles", {}).setdefault(ref, [])
    record["fact"]["entity_roles"][ref] = sorted((set(roles) - set(remove)) | set(add))
    for anchor in record.get("anchors", []):
        if anchor.get("purpose") == "mention" and anchor.get("entity_ref") == ref:
            anchor["roles"] = sorted((set(anchor.get("roles", [])) - set(remove)) | set(add))


def _synchronize_compatibility(exported, sample):
    """Keep current derived assessment consistent while retaining its old one."""
    result = copy.deepcopy(exported)
    companion = result["biomed_companion"]
    previous = copy.deepcopy(sample["record"].get("assessment", {}).get("compatibility", {}))
    companion.setdefault("provenance", {}).setdefault(VERSION, {})["original_compatibility"] = previous
    companion["provenance"][VERSION]["compatibility_refreshed_after_export"] = True
    companion.setdefault("assessment", {})["compatibility"] = {
        "status": result["compatibility_status"], "issues": copy.deepcopy(result["mapping_issues"]),
        "contract_validation": copy.deepcopy(result["contract_validation"]), "submit_ready": False,
    }
    return result


def _enrich_case_regimen(sample, old, *, dictionary, gene_standard):
    """Recover a single case's explicit oral daily regimen without guessing."""
    export, linked = old.get("derived_export"), old.get("linked_fact") or {}
    if not export or not linked or export.get("entity_operations"):
        return None
    record = export["biomed_companion"]
    fact = record.get("fact", {})
    if (not re.search(r"\bcase\b", str(fact.get("scope") or ""), re.IGNORECASE)
            or not re.search(r"\bpatient\b", str(fact.get("population") or ""), re.IGNORECASE)):
        return None
    intervention = linked.get("intervention") or {}
    entities = {item["ref"]: item for item in record.get("entities", [])}
    drug = entities.get(intervention.get("entity_ref"), {})
    name = drug.get("label") if drug.get("kind") == "drug" else None
    treatment = str(fact.get("treatment") or "")
    dose_match = re.fullmatch(rf"{re.escape(str(name or ''))}\s+(\d+(?:\.\d+)?)\s*mg", treatment, re.IGNORECASE)
    if not name or not dose_match:
        return None
    evidence_index(sample["record"], sample["source_document"])
    dose = dose_match.group(1)
    pattern = re.compile(rf"\bthe\s+patient\s+was\s+started\s+on\s+{re.escape(name)}\b"
                         rf"[^.;]{{0,180}}\b{re.escape(dose)}\s*mg\b[^.;]{{0,100}}"
                         r"\b(?:by mouth|orally)\s+(?:once\s+)?daily\b", re.IGNORECASE)
    matches = [p for p in sample["source_document"]["passages"] if pattern.search(p.get("text") or "")]
    if len(matches) != 1:
        return None
    source = matches[0]
    derived = copy.deepcopy(record)
    old_fact = copy.deepcopy(fact)
    known = evidence_index(derived, sample["source_document"])
    aliases = [alias for alias, item in known.items() if item["passage_id"] == str(source["id"])]
    dose_alias = aliases[0] if aliases else _add_anchor(derived, source)
    derived["fact"]["treatment"] = f"{name} {dose} mg by mouth daily"
    conditions = derived["fact"].get("conditions")
    if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
        return None
    original_index = evidence_index(sample["record"], sample["source_document"])
    extras = []
    for anchor in original_index.values():
        for rule in (r"\bafter\s+\d+\s+lines?\s+of\s+previous\s+therapy\b",
                     r"\boff[- ]label\s+therapy\b", r"\bStage\s+[IVX0-9]{1,6}[A-Z0-9]{0,3}\b"):
            hit = re.search(rule, anchor["quote"], re.IGNORECASE)
            if hit and hit.group() not in extras:
                extras.append(hit.group())
    for text in extras:
        if not any(value.casefold() == text.casefold() for value in conditions):
            conditions.append(text)
    if dose_alias not in derived["fact"].setdefault("evidence", []):
        derived["fact"]["evidence"].append(dose_alias)
    derived.setdefault("provenance", {}).setdefault(VERSION, {})["source_regimen_enrichment"] = {
        "original_fact": old_fact, "dose_source": {"passage_id": source["id"], "quote": source["text"]},
        "additional_conditions": extras, "semantic_status": "unreviewed"}
    derived["assessment"]["eligibility"] = assess_record(derived)
    adaptation = export["adaptation"]
    mapping = {"basis": adaptation.get("mapping_basis", ""), "entity_ids": copy.deepcopy(adaptation.get("entity_ids", {})),
               "unresolved_entity_refs": copy.deepcopy(adaptation.get("unresolved_entity_refs", [])),
               "identity_diagnostics": copy.deepcopy(adaptation.get("identity_diagnostics", {})),
               "trigger_scope": copy.deepcopy(adaptation["trigger_scope"]),
               "treatment_scope": copy.deepcopy(adaptation["treatment_scope"]),
               "trigger_bindings": copy.deepcopy(adaptation["trigger_bindings"])}
    mapping["trigger_scope"]["unstructured_clinical_context"] = copy.deepcopy(conditions)
    mapping["trigger_scope"]["clinical_context_enforced_by_target"] = False
    mapping["treatment_scope"]["dose_enforced_by_target"] = False
    mapping["treatment_scope"].setdefault("source_support", []).append({"passage_id": source["id"], "quote": source["text"]})
    for group in mapping["treatment_scope"].get("groups", []):
        group["label"] = derived["fact"]["treatment"]
    updated = export_record(derived, source_run=f"source-regimen-preview:{sample.get('batch_id', 'unknown')}",
                            dictionary=dictionary, mapping=mapping, gene_standard=gene_standard)
    if not updated["offline_ready"] or updated["target_readiness"]["status"] != "offline_ready":
        return None
    return _synchronize_compatibility(updated, sample)


def repair(sample, payload, *, dictionary, gene_standard, disease_nodes, ontology_source):
    """Preserve baseline, or attempt one tightly bounded source-backed repair."""
    old = baseline_preview(sample, payload, dictionary=dictionary, gene_standard=gene_standard,
                           disease_nodes=disease_nodes, ontology_source=ontology_source)
    base = {**old, "repair_version": VERSION, "repair_status": "baseline_preserved"}
    if old["status"] == "offline_structure_only" and old.get("derived_export"):
        enriched = _enrich_case_regimen(sample, old, dictionary=dictionary, gene_standard=gene_standard)
        base["derived_export"] = enriched or _synchronize_compatibility(old["derived_export"], sample)
        if enriched:
            base["repair_status"] = "source_regimen_enriched"
        return base
    if old["reason"] != "original_participant_or_event_scope_ambiguous":
        return base
    linked, record = old.get("linked_fact"), sample["record"]
    context = study_context(sample, linked)
    if (not linked or linked.get("status") != "source_linked" or context["review_primary_conflict"]
            or context["preclinical_population_explicit_in_fact_and_evidence"]):
        return base
    event, intervention = linked.get("molecular_event") or {}, linked.get("intervention") or {}
    if (linked.get("relation") != "treatment_outcome" or linked.get("source_scope") != "primary"
            or linked.get("same_cohort") is not True or event.get("event_type") != "mutation"
            or event.get("status") != "present" or event.get("role") != "eligibility"
            or intervention.get("role") != "current"):
        return base
    source_gene = str(event.get("gene") or "").upper()
    gene = source_gene if source_gene in gene_standard.get("symbols", {}) else gene_standard.get("aliases", {}).get(source_gene)
    variants = {m.group(1).upper() for m in VARIANT.finditer(str(event.get("event") or ""))}
    if len(variants) != 1 or not gene or gene not in gene_standard.get("symbols", {}):
        return base
    variant = next(iter(variants))
    entities = {e["ref"]: e for e in record.get("entities", [])}
    refs = event.get("entity_refs") or []
    gene_refs = [ref for ref in refs if ref in entities and entities[ref].get("kind") == "gene"
                 and entities[ref].get("label", "").upper() == source_gene]
    variant_refs = [ref for ref in refs if ref in entities and entities[ref].get("kind") == "variant"
                    and re.fullmatch(rf"(?:p\.)?{re.escape(variant)}", entities[ref].get("label", ""), re.IGNORECASE)]
    if len(gene_refs) != 1 or len(variant_refs) != 1 or set(refs) != set(gene_refs + variant_refs):
        return base
    roles = _roles(record)
    old_molecular = {ref for ref, values in roles.items() if "molecular" in values}
    if not old_molecular or not old_molecular.issubset(set(refs)):
        return base
    drug_ref = intervention.get("entity_ref")
    drugs = [ref for ref, values in roles.items() if "intervention" in values and ref in entities]
    if (len(drugs) != 1 or drug_ref != drugs[0] or entities[drug_ref].get("kind") != "drug"
            or str(record.get("fact", {}).get("treatment") or "").casefold() != entities[drug_ref]["label"].casefold()):
        return base
    disease_ref = linked.get("disease", {}).get("entity_ref")
    mapping = copy.deepcopy(sample["original_mapping"])
    aliases = _alias_ref(record, mapping, disease_ref, entities)
    if aliases is None or any(ref in mapping.get("unresolved_entity_refs", []) for ref in [disease_ref, *aliases]):
        return base
    if record.get("assessment", {}).get("execution", {}).get("status") != "complete":
        return base
    fact = record.get("fact", {})
    if (fact.get("relation") != "treatment_outcome" or not isinstance(fact.get("conditions"), list)
            or any(not isinstance(value, str) for value in fact["conditions"]) or fact.get("comparator")
            or re.search(r"\b(?:in vitro|cell|xenograft|pdx|animal)\b", str(fact.get("scope") or ""), re.IGNORECASE)):
        return base
    old_conditions = " ".join([str(fact.get("population") or ""), *fact["conditions"]])
    extra_genes = {match.group(1).upper() for match in re.finditer(
        r"(?<![\w-])([A-Z][A-Z0-9-]{1,19})\s+(?:mutations?|mutated|mutant|alterations?|altered)\b", old_conditions)
        if (match.group(1).upper() in gene_standard.get("symbols", {})
            or match.group(1).upper() in gene_standard.get("aliases", {}))
        and match.group(1).upper() not in {gene, source_gene}}
    if ({m.group(1).upper() for m in VARIANT.finditer(old_conditions)} - {variant}
            or extra_genes
            or re.search(r"\b(?:PD[- ]?L1|TMB|MSI|VAF|ctDNA|co[- ]occurring|wild[- ]type)\b", old_conditions, re.IGNORECASE)):
        return base
    results = fact.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return base
    value = results[0].get("value")
    result_group = results[0].get("group")
    if (not isinstance(result_group, str) or not result_group.strip()
            or re.search(r"\b(?:second|third|later)[- ]line\b", result_group, re.IGNORECASE)):
        return base
    if not isinstance(value, str) or not value.strip() or value.casefold() not in str(linked["outcome"].get("text") or "").casefold():
        return base
    index = evidence_index(record, sample["source_document"])
    source_result = index.get(results[0].get("passage"))
    if not source_result or value.casefold() not in source_result["quote"].casefold():
        return base
    conditions = linked.get("conditions", [])
    first_line = any(re.fullmatch(r"first[- ]line(?:\s+therapy)?", str(c), re.IGNORECASE) for c in conditions)
    if not first_line or any(not (re.fullmatch(r"first[- ]line(?:\s+therapy)?", str(c), re.IGNORECASE)
                                  or (_contains(str(c), variant) and re.search(r"\bmutation\b", str(c), re.IGNORECASE)))
                             for c in conditions):
        return base
    population_text = str(linked.get("population", {}).get("text") or "")
    population_match = re.match(r"^(.+?)\s+harboring\b", population_text, re.IGNORECASE)
    population = population_match.group(1).strip() if population_match else ""
    population_support = next((item for item in linked.get("population", {}).get("evidence", [])
                               if population and population.casefold() in item.get("quote", "").casefold()), None)
    if not population_support:
        return base
    drug_name = entities[drug_ref]["label"]
    other_drugs = [item.get("label") for item in entities.values()
                   if item.get("kind") == "drug" and item.get("label")
                   and item["label"].casefold() != drug_name.casefold()]
    support = _outcome_source(linked, variant, drug_name, value, first_line, other_drugs=other_drugs)
    gene_sources = _gene_variant_source(sample["source_document"], source_gene, variant, gene_standard)
    if not support or len(gene_sources) != 1:
        return base
    derived = copy.deepcopy(record)
    gene_passage = gene_sources[0]
    source_aliases = [alias for alias, anchor in index.items()
                      if anchor["passage_id"] == str(gene_passage["id"]) and anchor["quote"] == gene_passage["text"]]
    gene_alias = source_aliases[0] if source_aliases else _add_anchor(derived, gene_passage)
    for ref in aliases:
        _change_roles(derived, ref, add=("context",), remove=("disease",))
    _change_roles(derived, gene_refs[0], add=("molecular",))
    if isinstance(derived["fact"].get("entity_ids"), list) and gene_refs[0] not in derived["fact"]["entity_ids"]:
        derived["fact"]["entity_ids"].append(gene_refs[0])
    existing_conditions = derived["fact"]["conditions"]
    if not any(re.fullmatch(r"first[- ]line(?:\s+therapy)?", str(c), re.IGNORECASE) for c in existing_conditions):
        existing_conditions.append("first-line therapy")
    if not any(str(c).casefold() == population.casefold() for c in existing_conditions):
        existing_conditions.append(population)
    design_support = next((item for item in linked.get("population", {}).get("evidence", [])
                           if re.search(r"\bthis retrospective (?:study|analysis)\b", item.get("quote", ""), re.IGNORECASE)), None)
    if design_support and not re.search(r"\bretrospective\b", str(derived["fact"].get("scope") or ""), re.IGNORECASE):
        derived["fact"]["scope"] = str(derived["fact"].get("scope") or "original-study") + "; retrospective study"
    derived_index = evidence_index(derived, sample["source_document"])
    outcome_alias = None
    population_alias = None
    for alias, anchor in derived_index.items():
        if anchor["passage_id"] == support["passage_id"] and anchor["quote"] == support["quote"]:
            outcome_alias = alias
        if anchor["passage_id"] == population_support["passage_id"] and anchor["quote"] == population_support["quote"]:
            population_alias = alias
    if not outcome_alias or not population_alias:
        return base
    derived["fact"]["evidence"] = list(dict.fromkeys((population_alias, outcome_alias, gene_alias)))
    derived_result = derived["fact"]["results"][0]
    derived_result["passage"] = outcome_alias
    if not re.search(r"\bfirst[- ]line\b", result_group, re.IGNORECASE):
        derived_result["group"] = f"{result_group}; first-line therapy"
    if (re.fullmatch(r"PFS", str(derived_result.get("measure") or ""), re.IGNORECASE)
            and re.search(r"\bmedian\s+PFS\b", support["quote"], re.IGNORECASE)
            and re.search(r"\bmedian\s+PFS\b", str(linked["outcome"].get("text") or ""), re.IGNORECASE)):
        derived_result["measure"] = "median PFS"
    derived.setdefault("provenance", {})[VERSION] = {
        "source_sample_sha256": sample.get("sample_sha256"), "original_fact": copy.deepcopy(fact),
        "original_anchors": copy.deepcopy(record.get("anchors", [])), "linked_model_fact": copy.deepcopy(linked),
        "gene_variant_support": {"passage_id": gene_passage["id"], "quote": gene_passage["text"]},
        "cohort_outcome_support": copy.deepcopy(support), "normalized_disease_aliases": aliases,
        "population_support": copy.deepcopy(population_support),
        "study_design_support": copy.deepcopy(design_support),
        "added_conditions": ["first-line therapy", population], "semantic_status": "unreviewed",
        "automatic_clinical_matching": False,
    }
    derived["assessment"]["eligibility"] = assess_record(derived)
    condition_refs = [*gene_refs, *variant_refs]
    gene_support = [{"passage_id": gene_passage["id"], "quote": gene_passage["text"]}]
    outcome_support = [{"passage_id": support["passage_id"], "quote": support["quote"]}]
    mapping["trigger_scope"] = {"kind": "direct", "logic": "single", "condition_entity_refs": condition_refs,
        "non_trigger_entity_refs": [], "source_support": gene_support,
        "unstructured_clinical_context": copy.deepcopy(existing_conditions), "clinical_context_enforced_by_target": False,
        "reason": "One source-defined gene/variant; first-line context remains descriptive and is not enforced."}
    mapping["trigger_bindings"] = [{"entity_refs": condition_refs,
        "basis": "Source-defined gene mutation and exact variant; source article and variant numbering retained.",
        "source_support": gene_support, "gene_standard_identity": copy.deepcopy(gene_standard["symbols"][gene]),
        "gene_standard_source": copy.deepcopy(gene_standard["source"]),
        "input": {"trigger_type": "direct", "event_type": "mutation", "gene_symbol": gene, "target_key": variant}}]
    mapping["treatment_scope"] = {"kind": "single", "entity_refs": [drug_ref],
        "groups": [{"entity_refs": [drug_ref], "label": fact["treatment"]}],
        "source_support": outcome_support, "dose_enforced_by_target": False,
        "reason": "Single treatment and exact outcome located together; outcome is not generalized."}
    exported = export_record(derived, source_run=f"source-repair-preview:{sample.get('batch_id', 'unknown')}",
                             dictionary=dictionary, mapping=mapping, gene_standard=gene_standard)
    exported = _synchronize_compatibility(exported, sample)
    ready = exported["offline_ready"] and exported["target_readiness"]["status"] == "offline_ready"
    return {**base, "status": "offline_structure_only" if ready else "deferred",
            "reason": "semantic_and_first_line_matching_not_established" if ready else "target_contract_not_passed",
            "repair_status": "repaired_structure" if ready else "target_contract_not_passed",
            "derived_export": exported, "repair_gene": gene, "repair_variant": variant,
            "semantic_status": "unreviewed", "automatic_clinical_matching": False,
            "target_contacted": False, "target_written": False}
