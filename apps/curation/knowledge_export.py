"""Offline evidence-center exports, preserving biomed knowledge and source lineage.

This adapter never contacts tumor_api, invents target IDs, or gates extraction on
semantic review. ``offline_validated`` means the frozen source contract and a
provided target dictionary snapshot passed; it does not mean production import.
"""

import copy
import hashlib
import json
import re
import uuid
from collections import Counter

from apps.curation.knowledge_target_readiness import readiness_summary, target_readiness
from apps.curation.target_entity_sync import operation_index

SCHEMA = "biomed.tumor-api-export.v1"
CONTRACT = {
    "version": "biomed.target-contract-assessment.v4",
    "target_commit": "1ba2ba69c06c83edc3d5eb61b306369a0a7ca239",
    "serializer_sha256": "1488ddf24903b5ddea32b77b666d61cce230ca9a9f5e7e5a2bb8169e7c9a2f01",
    "validation": "offline_source_contract_not_live_serializer",
}
CATEGORIES = {"", "diagnostic", "progression", "prognostic", "hereditary", "treatment"}
ROOT_FIELDS = {
    "evidence_description", "evidence_description_en", "evidence_category",
    "evidence_source", "evidence_level", "evidence_type", "evidence_updated_at",
    "comment", "is_active", "conclusion_inputs",
}
ROLE_NAMES = {"disease", "intervention", "comparator", "molecular", "context"}
IDENTITY_DIAGNOSTICS = {
    "local_rule_unresolved", "local_entity_unavailable", "qualifier_source_missing", "main_cancer_unavailable",
    "target_uuid_not_in_snapshot", "target_name_ambiguous", "target_identity_missing",
    "local_identity_target_missing", "target_addition_request", "target_class_identity_missing",
    "target_addition_invalid", "target_entity_sync_failed",
}


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _key(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _issue(code, path, detail):
    return {"code": code, "path": path, "detail": detail}


def _dictionary(snapshot):
    """An explicit snapshot is required; ontology IDs cannot stand in for UUIDs."""
    if snapshot is None:
        return {"drugs": {}, "tumors": {}, "rules": {}}
    if snapshot.get("schema_version") != "tumor-api-dictionary-v1":
        raise ValueError("Unsupported target dictionary schema")
    if not isinstance(snapshot.get("source"), dict) or not snapshot["source"].get("snapshot_id"):
        raise ValueError("Target dictionary must identify its source snapshot")
    indexed = {}
    for kind in ("drugs", "tumors", "rules"):
        rows = snapshot.get(kind, [])
        if not isinstance(rows, list):
            raise TypeError(f"Dictionary {kind} must be an array")
        indexed[kind] = {}
        for row in rows:
            pk = _uuid(row.get("id"))
            if not pk or pk in indexed[kind]:
                raise ValueError(f"Invalid or duplicate target {kind} UUID")
            indexed[kind][pk] = row
    return indexed


def _trigger_key(trigger):
    event = str(trigger.get("event_type") or "").strip().lower()
    gene = str(trigger.get("gene_symbol") or "").strip().upper()
    target = re.sub(r"\s+", " ", str(trigger.get("target_key") or "")).strip()
    # Match the target canonicalization for the two supported normalized forms.
    if event == "cnv":
        if target.casefold() in {"amplification", "amplication", "amp", "dup", "duplication", "扩增"}:
            target = "Amplification"
        elif target.casefold() in {"deletion", "del", "loss", "缺失"}:
            target = "Deletion"
    elif event == "fusion":
        target = target.replace("–", "-").replace("—", "-")
        if target.casefold() in {"fusion", "fusions", gene.casefold(),
                                 f"{gene.casefold()}-fusion", f"{gene.casefold()}-fusions",
                                 f"{gene.casefold()} fusion", f"{gene.casefold()} fusions"}:
            target = "Fusion"
        else:
            target = re.sub(r"\s+FUSIONS?$", "", target, flags=re.IGNORECASE).strip()
            if target.casefold() == gene.casefold():
                target = "Fusion"
            elif "::" not in target and target.count("-") == 1:
                target = target.replace("-", "::")
    return (str(trigger.get("trigger_type") or ""), event, gene, target,
            _uuid(trigger.get("target_rule_id")) or "")


def validate_target_payload(payload, dictionary=None, *, entity_operations=None):
    """Validate supported create fields and cross-field constraints, offline.

    ``errors`` are contract failures; ``unresolved`` means target existence/state
    cannot be established. With explicit entity_operations this checks a plan
    template, whose references must be replaced by returned IDs before sending.
    Production uniqueness is always left to the target.
    """
    index = _dictionary(dictionary)
    operations = operation_index(entity_operations or [])
    errors, unresolved = [], []
    if not isinstance(payload, dict):
        return {"valid": False, "errors": [_issue("object_required", "$", "Payload must be an object")],
                "unresolved": []}
    for field in set(payload) - ROOT_FIELDS:
        errors.append(_issue("unknown_field", field, "Not a target create field"))
    for field, maximum in (("evidence_description", None), ("evidence_description_en", None),
                           ("evidence_source", 512), ("evidence_type", 255),
                           ("evidence_updated_at", 64), ("comment", None)):
        value = payload.get(field, "")
        if not isinstance(value, str) or (maximum and len(value) > maximum):
            errors.append(_issue("invalid_string", field, "Wrong type or maximum length exceeded"))
    if not isinstance(payload.get("evidence_description"), str) or not payload["evidence_description"].strip():
        errors.append(_issue("required_description", "evidence_description", "Nonempty description is required"))
    category = payload.get("evidence_category", "")
    if not isinstance(category, str) or category not in CATEGORIES:
        errors.append(_issue("invalid_category", "evidence_category", "Unsupported target category"))
    level = payload.get("evidence_level", "-")
    if not isinstance(level, str) or level not in {"-", "A", "B", "C", "D"}:
        errors.append(_issue("invalid_level", "evidence_level", "Unsupported target level"))
    if "is_active" in payload and type(payload["is_active"]) is not bool:
        errors.append(_issue("invalid_boolean", "is_active", "Must be boolean"))
    conclusions = payload.get("conclusion_inputs")
    if not isinstance(conclusions, list) or not conclusions:
        errors.append(_issue("required_conclusion", "conclusion_inputs", "At least one conclusion is required"))
        conclusions = []
    seen_conclusions = set()
    for number, conclusion in enumerate(conclusions):
        path = f"conclusion_inputs[{number}]"
        if not isinstance(conclusion, dict):
            errors.append(_issue("object_required", path, "Conclusion must be an object"))
            continue
        if set(conclusion) - {"potency", "drug_ids", "disease_scope_inputs", "trigger_inputs"}:
            errors.append(_issue("unknown_field", path, "Unknown conclusion field"))
        potency = conclusion.get("potency", "-")
        if not isinstance(potency, str) or potency not in {"-", "S", "R", "C"}:
            errors.append(_issue("invalid_potency", path, "Unsupported potency"))
        values = {}
        for field in ("drug_ids", "disease_scope_inputs", "trigger_inputs"):
            values[field] = conclusion.get(field, [])
            if not isinstance(values[field], list):
                errors.append(_issue("array_required", f"{path}.{field}", "Must be an array"))
                values[field] = []
        drugs, scopes, triggers = (values[k] for k in ("drug_ids", "disease_scope_inputs", "trigger_inputs"))
        if category != "treatment" and (drugs or potency != "-"):
            errors.append(_issue("treatment_only", path, "Only treatment evidence permits drugs or potency"))
        drug_keys, scope_keys, trigger_keys = [], [], []

        def target_id(value, kind, at):
            if isinstance(value, dict) and set(value) == {"$entity_operation"}:
                key = value["$entity_operation"]
                if isinstance(key, str) and key in operations and kind == "drugs":
                    return key
                errors.append(_issue("invalid_entity_operation", at, "Unknown or wrong-kind automatic entity prerequisite"))
                return None
            pk = _uuid(value)
            if not pk:
                errors.append(_issue("invalid_target_uuid", at, "Requires a target UUID, not an ontology or local ID"))
            elif pk not in index[kind]:
                unresolved.append(_issue("target_id_unresolved", at, f"UUID absent from {kind} snapshot"))
            return pk

        for i, value in enumerate(drugs):
            drug_keys.append(target_id(value, "drugs", f"{path}.drug_ids[{i}]"))
        for i, scope in enumerate(scopes):
            at = f"{path}.disease_scope_inputs[{i}]"
            if not isinstance(scope, dict) or set(scope) - {"tumor_id", "subtype_id"}:
                errors.append(_issue("invalid_disease_scope", at, "Expected tumor_id and/or subtype_id"))
                continue
            if not scope.get("tumor_id") and not scope.get("subtype_id"):
                errors.append(_issue("empty_disease_scope", at, "At least one tumor UUID is required"))
            scope_keys.append(tuple(target_id(scope[k], "tumors", f"{at}.{k}") if scope.get(k) else ""
                                    for k in ("tumor_id", "subtype_id")))
        if not triggers:
            errors.append(_issue("required_trigger", f"{path}.trigger_inputs", "Every conclusion requires a molecular trigger"))
        for i, trigger in enumerate(triggers):
            at = f"{path}.trigger_inputs[{i}]"
            if not isinstance(trigger, dict):
                errors.append(_issue("invalid_trigger", at, "Trigger must be an object"))
                continue
            if set(trigger) - {"trigger_type", "event_type", "gene_symbol", "target_key", "target_rule_id"}:
                errors.append(_issue("unknown_field", at, "Unknown trigger field"))
            for field, maximum in (("gene_symbol", 64), ("target_key", 255), ("event_type", None)):
                value = trigger.get(field, "")
                if not isinstance(value, str) or (maximum and len(value) > maximum):
                    errors.append(_issue("invalid_string", f"{at}.{field}", "Invalid trigger field"))
            raw_event = trigger.get("event_type", "")
            if not isinstance(raw_event, str) or raw_event not in {"", "mutation", "cnv", "fusion"}:
                errors.append(_issue("invalid_event_type", f"{at}.event_type", "Unknown target event type"))
            kind, event, gene, target, rule_id = _trigger_key(trigger)
            if kind == "direct":
                if event not in {"mutation", "cnv", "fusion"} or not gene or not target or trigger.get("target_rule_id"):
                    errors.append(_issue("invalid_direct_trigger", at, "Direct trigger requires event, gene, target and no rule"))
            elif kind == "rule":
                pk = target_id(trigger.get("target_rule_id"), "rules", f"{at}.target_rule_id")
                if event or gene or target:
                    errors.append(_issue("mixed_trigger", at, "Rule trigger cannot also specify direct fields"))
                rule = index["rules"].get(pk)
                if rule and not (rule.get("review_status") == "approved" and rule.get("is_active") is True
                                 and rule.get("is_locked") is False):
                    errors.append(_issue("unusable_rule", at, "Target rule must be approved, active and unlocked"))
            else:
                errors.append(_issue("invalid_trigger_type", at, "Expected direct or rule"))
            trigger_keys.append((kind, event, gene, target, rule_id))
        for field, keys in (("drug_ids", drug_keys), ("disease_scope_inputs", scope_keys),
                            ("trigger_inputs", trigger_keys)):
            if len(set(keys)) != len(keys):
                errors.append(_issue("duplicate_items", f"{path}.{field}", "Duplicate items are rejected"))
        identity = digest([potency, sorted(drug_keys, key=str), sorted(scope_keys, key=str), sorted(trigger_keys, key=str)])
        if identity in seen_conclusions:
            errors.append(_issue("duplicate_conclusion", path, "Identical conclusions are rejected"))
        seen_conclusions.add(identity)
    return {"valid": not errors and not unresolved, "errors": errors, "unresolved": unresolved}


def _roles(record):
    explicit = record.get("fact", {}).get("entity_roles", {})
    if not isinstance(explicit, dict):
        explicit = {}
    result = {ref: set(roles) & ROLE_NAMES for ref, roles in explicit.items() if isinstance(roles, list)}
    for anchor in record.get("anchors", []):
        if anchor.get("purpose") == "mention" and anchor.get("entity_ref"):
            result.setdefault(anchor["entity_ref"], set()).update(set(anchor.get("roles", [])) & ROLE_NAMES)
    return result


def _description(fact):
    # Deterministic rendering of stored fields, not a new model-written claim.
    labels = (("conclusion", "Conclusion"), ("population", "Population"), ("treatment", "Treatment"), ("relation", "Relation"),
              ("direction", "Direction"), ("comparator", "Comparator"),
              ("conditions", "Conditions"), ("molecular_logic", "Molecular logic"),
              ("results", "Reported results"), ("scope", "Study scope"))
    parts = []
    for key, label in labels:
        value = fact.get(key)
        if value:
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
            parts.append(f"{label}: {rendered}")
    return "\n".join(parts)


def _source_supported(record, support):
    """Validate the mapping author's exact source excerpts, not their interpretation."""
    if not isinstance(support, list) or not support:
        return False
    anchors = record.get("anchors", [])
    return all(isinstance(item, dict) and isinstance(item.get("quote"), str) and item["quote"]
               and any(a.get("passage_id") == item.get("passage_id")
                       and item["quote"] in a.get("quote", "") for a in anchors)
               for item in support)


def _target_terms(event, value):
    """Match concrete mutation-key intersections; generic mutation is not a wildcard."""
    if event == "mutation":
        return {re.sub(r"^p\.(?=[A-Za-z*])", "", term.strip(), flags=re.IGNORECASE).casefold()
                for term in re.split(r"[;；,，]", str(value or "")) if term.strip()}
    return {_trigger_key({"trigger_type": "direct", "event_type": event, "target_key": value})[3].casefold()}


def _rule_signature(expression, conditions):
    """Compare source-normalized ASTs and conditions without equating AND with OR."""
    if not isinstance(conditions, list) or len(conditions) < 2:
        raise ValueError("A compound rule needs at least two explicit conditions")
    by_code = {}
    for condition in conditions:
        code = condition.get("code")
        if not isinstance(code, str) or not code or code in by_code:
            raise ValueError("Rule condition codes must be unique")
        _, event, gene, key, _ = _trigger_key({"trigger_type": "direct", **condition})
        if event not in {"mutation", "cnv", "fusion"} or not gene or not key:
            raise ValueError("Rule condition has no explicit logical target")
        by_code[code] = (event, gene, tuple(sorted(_target_terms(event, key))), bool(condition.get("distinct_event", False)))
    referenced, nodes = set(), [0]

    def walk(node, depth=1):
        nodes[0] += 1
        if not isinstance(node, dict) or depth > 8 or nodes[0] > 100:
            raise ValueError("Invalid or oversized rule AST")
        if set(node) == {"condition"}:
            code = node["condition"]
            if code not in by_code:
                raise ValueError("Unknown rule condition")
            referenced.add(code)
            return ("condition", by_code[code])
        op, children = node.get("op"), node.get("children")
        if set(node) != {"op", "children"} or op not in {"all", "any", "not"} or not isinstance(children, list):
            raise ValueError("Invalid rule operator")
        if (op == "not" and len(children) != 1) or (op != "not" and len(children) < 2):
            raise ValueError("Invalid rule arity")
        return (op, tuple(sorted((walk(child, depth + 1) for child in children), key=repr)))

    result = walk(expression)
    if referenced != set(by_code):
        raise ValueError("Rule has unreferenced conditions")
    return result


def _gene_terms(gene, gene_standard=None):
    """Optional, unambiguous official-standard aliases; never a model synonym."""
    if not gene_standard:
        return [gene]
    identity = gene_standard.get("symbols", {}).get(gene)
    source = gene_standard.get("source", {})
    if (not identity or identity.get("symbol", "").upper() != gene
            or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))):
        return [gene]
    return [gene, *[alias for alias, symbol in gene_standard.get("aliases", {}).items()
                   if symbol == gene and alias not in gene_standard["symbols"]]]


def _matching_check(binding, dictionary, record, condition_refs, gene_standard=None):
    target = binding.get("input", {})
    kind, event, gene, key, rule_id = _trigger_key(target)
    result = {"status": "unverified", "snapshot_consistency": "unverified",
              "live_sample_matching": "unverified", "input": copy.deepcopy(target), "issues": [], "warnings": []}
    labels = [e["label"] for e in record.get("entities", []) if e["ref"] in condition_refs]

    def source_identifies(event, gene, target_key):
        gene_present = bool(gene) and any(re.search(rf"(?<!\w){re.escape(term)}(?=p\.[A-Za-z]|\b)", label, re.IGNORECASE)
                                         for term in _gene_terms(gene, gene_standard) for label in labels)
        terms = _target_terms(event, target_key)
        variant_present = event != "mutation" or (bool(terms) and all(
            any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", label, re.IGNORECASE)
                for label in labels) for term in terms))
        return gene_present, variant_present

    if kind == "direct":
        if event == "mutation" and (_key(key) in {"mutation", "mutations", "mutant", "any", "*", "any mutation", "oncogenic mutation", "activating mutation"} or _key(key) == _key(gene)):
            result["issues"].append("generic_mutation_is_not_a_matchable_wildcard")
        gene_present, variant_present = source_identifies(event, gene, key)
        if not gene_present:
            result["issues"].append("source_condition_does_not_identify_this_gene")
        if not variant_present:
            result["issues"].append("source_condition_does_not_identify_this_variant_key")
        matches = []
        for row in (dictionary or {}).get("logical_targets", []):
            _, row_event, row_gene, row_key, _ = _trigger_key({"trigger_type": "direct", **row})
            if event == row_event and gene == row_gene and _target_terms(event, key) & _target_terms(event, row_key):
                matches.append({k: row.get(k) for k in ("event_type", "gene_symbol", "target_key", "source_table")})
        result["snapshot_matches"] = matches
        if not matches:
            # Existing evidence trigger keys are not a create-serializer
            # requirement. For a single exact protein substitution identified
            # by the source entities, an absent key is an unverified snapshot
            # observation, not proof that a new direct trigger is invalid.
            if (event == "mutation" and re.fullmatch(r"(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*]", key, re.IGNORECASE)
                    and gene_present and variant_present):
                support = binding.get("source_support")
                terms = _target_terms("mutation", key)
                existing_genes = {str(row.get("gene_symbol") or "").upper() for row in (dictionary or {}).get("logical_targets", [])}
                gene_record = binding.get("gene_standard_identity") or {}
                gene_source = binding.get("gene_standard_source") or {}
                local_gene_verified = (str(gene_record.get("symbol") or "").upper() == gene
                                       and re.fullmatch(r"[0-9]+", str(gene_record.get("gene_id") or ""))
                                       and re.fullmatch(r"[0-9]+", str(gene_record.get("hgnc_id") or ""))
                                       and re.fullmatch(r"[0-9a-f]{64}", str(gene_source.get("sha256") or "")))
                if gene not in existing_genes and not local_gene_verified:
                    result["issues"].append("new_key_gene_standard_provenance_required")
                elif (_source_supported(record, support) and any(
                    any(re.search(rf"(?<!\w){re.escape(term)}(?=p\.[A-Za-z]|\b)", item["quote"], re.IGNORECASE)
                        for term in _gene_terms(gene, gene_standard))
                    and all(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", item["quote"], re.IGNORECASE)
                            for term in terms) for item in support
                )):
                    result["snapshot_consistency"] = "not_covered"
                    result["warnings"].append("snapshot_not_covering_key")
                else:
                    result["issues"].append("new_key_source_support_required")
            elif event == "fusion" and gene_standard and key.casefold() == "fusion":
                # A target generic Fusion is a gene-level key, not an invented
                # partner. This opt-in path requires the detected rearrangement
                # as an exact source condition, a real gene and assay evidence.
                pattern = rf"\b{re.escape(gene)}\s+(?:gene\s+)?(?:rearrangement|fusion)\b"
                support = binding.get("source_support")
                qualified = [item["quote"] for item in (support or []) if re.search(pattern, item.get("quote", ""), re.IGNORECASE)]
                verified = (gene in gene_standard.get("symbols", {})
                            and _source_supported(record, support)
                            and (any(re.search(pattern, label, re.IGNORECASE) for label in labels)
                                 or (any(label.casefold() == gene.casefold() for label in labels)
                                     and re.search(pattern, json.dumps(record.get("fact", {})), re.IGNORECASE)))
                            and any(re.search(r"\b(?:FISH|fluorescence in situ hybridization|sequencing)\b", q, re.IGNORECASE)
                                    and not re.search(r"\b(?:no|not|negative|absent|without|suspected|possible)\b.{0,40}" + pattern, q, re.IGNORECASE)
                                    and not re.search(pattern + r"(?:\s+(?:was|were|is|are))?\s+(?:not\s+(?:detected|identified|confirmed)|negative|absent)\b", q, re.IGNORECASE)
                                    for q in qualified))
                if verified:
                    result["snapshot_consistency"] = "not_covered"
                    result["warnings"] += ["snapshot_not_covering_key", "gene_level_rearrangement_partner_unspecified"]
                else:
                    result["issues"].append("new_key_source_support_required")
            else:
                result["issues"].append("no_concrete_logical_target_match_in_snapshot")
    elif kind == "rule":
        rule = _dictionary(dictionary)["rules"].get(rule_id)
        if not rule:
            result["issues"].append("target_rule_missing_from_snapshot")
        else:
            try:
                expected = _rule_signature(binding.get("expected_expression"), binding.get("expected_conditions"))
                actual = _rule_signature(rule.get("expression"), rule.get("conditions"))
                if expected != actual:
                    result["issues"].append("source_condition_ast_differs_from_target_rule")
                for condition in binding["expected_conditions"]:
                    _, event, gene, key, _ = _trigger_key({"trigger_type": "direct", **condition})
                    if not all(source_identifies(event, gene, key)):
                        result["issues"].append("source_condition_does_not_identify_each_rule_target")
                        break
            except (ValueError, TypeError, KeyError) as exc:
                result["issues"].append(f"rule_equivalence_unverified: {exc}")
    else:
        result["issues"].append("invalid_trigger_type")
    if not result["issues"] and result["snapshot_consistency"] != "not_covered":
        result["snapshot_consistency"] = "consistent"
    result["scope_note"] = "Existing evidence logical keys/rules only; raw allele index/variant values and live sample matching are not validated"
    return result


def export_record(record, *, source_run, dictionary=None, mapping=None, gene_standard=None):
    """Map a stored fact; unresolved mappings stay explicit and retain all sources."""
    index, mapping = _dictionary(dictionary), mapping or {}
    operations = operation_index(mapping.get("entity_operations", []))
    operation_refs = mapping.get("entity_operation_refs", {})
    fact = record.get("fact", {})
    entities = {e["ref"]: e for e in record.get("entities", [])}
    roles = _roles(record)
    issues, resolved = [], {}
    molecular_refs = [ref for ref, items in roles.items() if "molecular" in items]
    trigger_scope = mapping.get("trigger_scope", {})
    treatment_scope = mapping.get("treatment_scope", {})
    condition_refs = trigger_scope.get("condition_entity_refs", [])
    non_trigger_refs = trigger_scope.get("non_trigger_entity_refs", [])
    treatment_refs = treatment_scope.get("entity_refs", [])
    scope_kind = trigger_scope.get("kind")
    treatment_kind = treatment_scope.get("kind")

    def scope_issue(code, scope, detail):
        issues.append(_issue(code, scope, detail))

    if scope_kind not in {"none", "unsupported_marker", "direct", "rule", "unresolved"}:
        scope_issue("trigger_condition_scope_unverified", "trigger_scope", "Identify necessary conditions separately from mechanisms, outcome markers and background molecular profiles")
    else:
        if not _source_supported(record, trigger_scope.get("source_support")):
            scope_issue("trigger_scope_source_unverified", "trigger_scope.source_support", "Every interpretation must retain exact excerpts from this record's anchors")
        if (not isinstance(condition_refs, list) or not isinstance(non_trigger_refs, list)
                or any(ref not in entities for ref in condition_refs + non_trigger_refs)
                or any("molecular" not in roles.get(ref, set()) for ref in condition_refs)
                or set(condition_refs) & set(non_trigger_refs)):
            scope_issue("invalid_trigger_scope", "trigger_scope", "Refs must exist and be disjoint; conditions require original molecular roles")
            condition_refs, non_trigger_refs = [], []
        if set(molecular_refs) - set(condition_refs) - set(non_trigger_refs):
            scope_issue("trigger_condition_scope_unverified", "trigger_scope", "Some molecular participants have not been classified as conditions or non-trigger context")
        if scope_kind == "none" and condition_refs:
            scope_issue("invalid_trigger_scope", "trigger_scope", "No-trigger scope cannot also specify conditions")
        if scope_kind in {"direct", "rule"} and not condition_refs:
            scope_issue("invalid_trigger_scope", "trigger_scope", "This scope requires source condition refs")
        if scope_kind == "unsupported_marker":
            scope_issue("unsupported_biomarker_condition", "trigger_scope", "The source condition is expression, protein, signature or another marker outside mutation/cnv/fusion triggers")
    # Missing scopes retain a legacy draft; roles alone never establish eligibility.
    if treatment_kind not in {"none", "single", "combination", "alternatives", "sequential", "unspecified"}:
        treatment_refs = [ref for ref, e in entities.items() if e["kind"] == "drug" and "intervention" in roles.get(ref, set())]
        if treatment_refs:
            scope_issue("treatment_scope_unverified", "treatment_scope", "Treatment participants need explicit single/combination/alternative/order interpretation")
    else:
        if not _source_supported(record, treatment_scope.get("source_support")):
            scope_issue("treatment_scope_source_unverified", "treatment_scope.source_support", "Treatment interpretation requires exact source excerpts")
        if (not isinstance(treatment_refs, list)
                or any(ref not in entities or entities[ref]["kind"] != "drug"
                       or "intervention" not in roles.get(ref, set()) for ref in treatment_refs)):
            scope_issue("invalid_treatment_scope", "treatment_scope", "Treatment refs must identify stored drug entities with original intervention roles")
            treatment_refs = []
        if ((treatment_kind == "none" and treatment_refs)
                or (treatment_kind == "single" and len(treatment_refs) != 1)):
            scope_issue("invalid_treatment_scope", "treatment_scope", "None requires no drug refs; single requires exactly one")
        if treatment_kind in {"alternatives", "sequential"}:
            scope_issue("treatment_requires_split", "treatment_scope", "Target drug_ids cannot encode alternatives or sequence; preserve groups without flattening")
        elif treatment_kind == "combination":
            scope_issue("treatment_combination_semantics_unverified", "treatment_scope", "Target drug_ids stores a set without a combination operator; the source regimen remains in companion data")
        elif treatment_kind == "unspecified":
            scope_issue("treatment_scope_unverified", "treatment_scope", "The treatment relation is not sufficiently specified for target adaptation")
    drug_ids, disease_scopes = [], []
    for ref, entity in entities.items():
        expected = "drugs" if ref in treatment_refs and entity["kind"] == "drug" else (
            "tumors" if "disease" in roles.get(ref, set()) and entity["kind"] == "disease" else None)
        if not expected:
            continue
        supplied = mapping.get("entity_ids", {}).get(ref)
        matches = []
        operation_key = operation_refs.get(ref)
        if operation_key and ref not in mapping.get("unresolved_entity_refs", []):
            if (expected != "drugs" or supplied or operation_key not in operations or not mapping.get("basis")):
                issues.append(_issue("invalid_entity_operation", ref, "Automatic creation requires one identified drug operation and no competing UUID"))
                continue
            operation = operations[operation_key]
            reference = {"$entity_operation": operation_key}
            if reference not in drug_ids:
                drug_ids.append(reference)
            resolved[ref] = {"target_id": None, "target_kind": "drugs", "operation_id": operation_key,
                             "method": "automatic_ensure_drug", "target_name": operation["create_payload"]["name_cn"]}
            continue
        if ref in mapping.get("unresolved_entity_refs", []):
            # An article-scoped local conflict must not be bypassed by the
            # snapshot's otherwise unique name match.
            if "identity_diagnostics" in mapping:
                diagnostics = mapping.get("identity_diagnostics")
                diagnostic = diagnostics.get(ref, {}) if isinstance(diagnostics, dict) else {}
                if not isinstance(diagnostic, dict):
                    diagnostic = {}
                code = diagnostic.get("status")
                if code not in IDENTITY_DIAGNOSTICS:
                    code = "target_identity_missing"
                issues.append(_issue(code, ref, diagnostic.get("detail") or "Target identity is unresolved"))
                continue
            issues.append(_issue("local_identity_unresolved", ref, "Local identity decision blocks automatic target-name matching"))
        elif supplied:
            if not mapping.get("basis"):
                issues.append(_issue("crosswalk_basis_pending", ref, "Explicit target UUID mapping requires its source basis"))
            if _uuid(supplied) in index[expected]:
                matches = [_uuid(supplied)]
            else:
                issues.append(_issue("target_id_unresolved", ref, "Explicit UUID is absent from the target snapshot"))
                if "identity_diagnostics" in mapping:
                    continue
        else:
            for pk, row in index[expected].items():
                names = [row.get(field, "") for field in ("name", "name_cn", "name_en", "name_en_abbr")]
                names += row.get("aliases", [])
                if _key(entity["label"]) and _key(entity["label"]) in {_key(name) for name in names}:
                    matches.append(pk)
        if len(matches) != 1:
            issues.append(_issue("target_identity_pending", ref, "No unique exact name or explicit target UUID mapping"))
            continue
        resolved[ref] = {"target_id": matches[0], "target_kind": expected,
                         "method": "explicit_crosswalk" if supplied else "unique_exact_target_name",
                         "target_name": index[expected][matches[0]].get("name", "")}
        if expected == "drugs":
            if matches[0] not in drug_ids:
                drug_ids.append(matches[0])
        elif {"tumor_id": matches[0]} not in disease_scopes:
            disease_scopes.append({"tumor_id": matches[0]})
    if not any(roles.values()):
        issues.append(_issue("participant_roles_pending", "fact.entity_roles", "Legacy selected entities are not evidence of relationship roles"))
    named_term_diagnostics = [{"name": name, "treatment_entity_refs": [ref for ref in treatment_refs if _key(entities[ref]["label"]) == _key(name)],
                               "note": "A named term can be a comparator, prior therapy, regimen string or background; it is not automatically a target drug"}
                              for name in fact.get("drug_names", [])]
    if treatment_kind in {"alternatives", "sequential"}:
        drug_ids = []
    bindings = mapping.get("trigger_bindings", [])
    triggers, matching_validation, bound_refs = [], [], set()
    for i, binding in enumerate(bindings):
        refs = binding.get("entity_refs", [])
        if (not refs or any(ref not in condition_refs for ref in refs)
                or scope_kind not in {"direct", "rule", "unresolved"} or not binding.get("basis")):
            issues.append(_issue("trigger_mapping_unverified", f"trigger_bindings[{i}]", "Requires necessary source condition refs and normalization basis; contextual molecular entities cannot become eligibility triggers"))
            continue
        triggers.append(copy.deepcopy(binding.get("input", {})))
        bound_refs.update(refs)
        check = _matching_check(binding, dictionary, record, refs, gene_standard)
        matching_validation.append(check)
        if check["issues"]:
            issues.append(_issue("trigger_matching_unverified", f"trigger_bindings[{i}]", "; ".join(check["issues"])))
    if set(condition_refs) - bound_refs and scope_kind in {"direct", "rule", "unresolved"}:
        issues.append(_issue("trigger_conditions_pending", "trigger_bindings", "Necessary source conditions have no target mapping"))
    logic = trigger_scope.get("logic", fact.get("molecular_logic"))
    if logic in {"and", "complex"} and (len(triggers) != 1 or any(t.get("trigger_type") != "rule" for t in triggers)):
        issues.append(_issue("compound_rule_required", "trigger_bindings", "Compound conditions require an existing target rule; a flat trigger list cannot replace AND"))
    # A gene mention and its separate amino-acid mention can describe the same
    # source condition. Only accept an exact, single direct substitution whose
    # binding covers all refs; arbitrary additional labels remain unresolved.
    same_substitution = False
    if len(condition_refs) > 1 and logic == "single" and len(bindings) == len(triggers) == 1:
        target = triggers[0]
        _, event, gene, key, _ = _trigger_key(target)
        if (target.get("trigger_type") == "direct" and event == "mutation"
                and re.fullmatch(r"(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*]", key, re.IGNORECASE)
                and set(bindings[0].get("entity_refs", [])) == set(condition_refs)):
            variant = re.sub(r"^p\.", "", key, flags=re.IGNORECASE)
            gene_pattern = "(?:" + "|".join(re.escape(term) for term in _gene_terms(gene, gene_standard)) + ")"
            variant_pattern = re.escape(variant)
            label_pattern = (rf"(?:{gene_pattern}(?:(?:\s+(?:p\.)?|p\.){variant_pattern}(?:\s+(?:mutation|variant))?)?"
                             rf"|(?:p\.)?{variant_pattern})")
            same_substitution = all(re.fullmatch(label_pattern, entities[ref].get("label", "").strip(), re.IGNORECASE)
                                    for ref in condition_refs)
    if len(condition_refs) > 1 and logic not in {"and", "or", "complex"} and not same_substitution:
        issues.append(_issue("condition_logic_unverified", "trigger_scope.logic", "Multiple source conditions require explicit logic; co-occurrence is not AND or OR"))
    if not triggers:
        issues.append(_issue("molecular_trigger_mapping_pending" if molecular_refs else "no_molecular_trigger",
                             "trigger_inputs", "Target requires a molecular trigger; never invent one for ordinary treatment or expression knowledge"))
    category = mapping.get("evidence_category")
    if category is None:
        category = "treatment" if fact.get("relation") == "treatment_outcome" else ""
        if not category:
            issues.append(_issue("category_mapping_pending", "evidence_category", "Relation does not uniquely identify a target evidence category"))
    article = record.get("article", {})
    source = "; ".join(f"{key.upper()}:{article[key]}" for key in ("pmid", "doi") if article.get(key))
    source += f"; biomed:{source_run}/{record['group_id']}"
    description = _description(fact)
    if molecular_refs:
        description += "\nMolecular participants: " + "; ".join(entities[ref]["label"] for ref in molecular_refs if ref in entities)
    payload = {"evidence_description": description, "evidence_description_en": description,
               "evidence_category": category, "evidence_source": source,
               "evidence_level": "-", "evidence_type": "", "is_active": False,
               "comment": f"biomed:{source_run}/{record['group_id']}; full provenance in companion record",
               "conclusion_inputs": [{"potency": "-", "drug_ids": drug_ids,
                                      "disease_scope_inputs": disease_scopes, "trigger_inputs": triggers}]}
    used_operations = {e["operation_id"] for e in resolved.values() if e.get("operation_id")}
    if set(operations) != used_operations or set(operation_refs) - set(resolved):
        issues.append(_issue("unused_entity_operation", "entity_operations", "Every automatic prerequisite must bind an actual treatment participant"))
    template = copy.deepcopy(payload) if used_operations else None
    validation = validate_target_payload(payload, dictionary, entity_operations=list(operations.values()))
    # The template is executable only after ensure_drug returns actual UUIDs.
    # Keep symbolic dependencies out of the ordinary API payload entirely.
    if template:
        payload["conclusion_inputs"][0]["drug_ids"] = [pk for pk in drug_ids if isinstance(pk, str)]
    issues.extend(validation["unresolved"])
    status = "offline_validated"
    legacy_without_condition = not trigger_scope and any(roles.values()) and not molecular_refs
    if (scope_kind in {"none", "unsupported_marker"} or legacy_without_condition) and not triggers:
        status = "not_representable"
    elif any(error["code"] != "required_trigger" for error in validation["errors"]):
        status = "invalid_payload"
    elif issues:
        status = "pending_mapping"
    elif validation["errors"]:
        status = "invalid_payload"
    article_anchors = [a for a in record.get("anchors", []) if a.get("purpose") == "evidence"]
    sources_valid = (
        _uuid(record.get("version_id"))
        and re.fullmatch(r"[0-9a-f]{64}", str(record.get("source_sha256") or ""))
        and article_anchors
        and all(_uuid(anchor.get("passage_id")) and isinstance(anchor.get("quote"), str)
                and anchor["quote"] and type(anchor.get("start")) is int
                and type(anchor.get("end")) is int and anchor["start"] >= 0
                and anchor["end"] - anchor["start"] == len(anchor["quote"])
                for anchor in article_anchors)
    )
    if not sources_valid:
        issues.append(_issue("missing_source_lineage", "anchors", "Version UUID, source SHA256 and well-formed evidence spans are required"))
        status = "invalid_source"
    if status == "offline_validated" and used_operations:
        status = "ready_after_entity_sync"
    preview_only = (dictionary or {}).get("source", {}).get("environment") == "local_preview"
    if status == "offline_validated" and preview_only:
        status = "preview_validated"
    readiness = target_readiness(record, status)
    if readiness["status"] == "outside_target" and status != "invalid_source":
        # Missing names or default "unverified" scopes must not turn a known
        # target-domain mismatch into an endless identity-mapping task.
        status = "not_representable"
    semantic_limits = ["Target has no structured combination/order, study-scope or original-span fields; retained in description and companion",
                       "Benefit is not automatically sensitivity; potency and evidence grade remain unspecified",
                       "Live serializer, current dictionary state and production duplicate checks have not run"]
    if trigger_scope.get("unstructured_clinical_context") or treatment_scope.get("dose_enforced_by_target") is False:
        semantic_limits.append("Clinical context, treatment history and dose remain descriptive; target trigger and disease scope do not enforce them")
    return {"group_id": record["group_id"], "compatibility_status": status,
            "target_readiness": readiness,
            "target_payload": payload, "resolved_entities": resolved,
            "target_payload_template": template, "entity_operations": list(operations.values()),
            "entity_sync_ready": status == "ready_after_entity_sync", "preview_only": preview_only,
            "preview_ready": status == "preview_validated",
            "mapping_issues": issues, "contract_validation": validation,
            "contract_validation_scope": "entity_operation_template" if template else "target_payload",
            "adaptation": {"trigger_scope": copy.deepcopy(trigger_scope), "treatment_scope": copy.deepcopy(treatment_scope),
                           "mapping_basis": mapping.get("basis", ""), "entity_ids": copy.deepcopy(mapping.get("entity_ids", {})),
                           "unresolved_entity_refs": copy.deepcopy(mapping.get("unresolved_entity_refs", [])),
                           **({"identity_diagnostics": copy.deepcopy(mapping["identity_diagnostics"])} if "identity_diagnostics" in mapping else {}),
                           **({"entity_sync_receipts": copy.deepcopy(mapping["entity_sync_receipts"])} if "entity_sync_receipts" in mapping else {}),
                           "trigger_bindings": copy.deepcopy(bindings)},
            "matching_validation": matching_validation, "live_sample_matching": "unverified",
            "named_term_diagnostics": named_term_diagnostics,
            "offline_ready": status == "offline_validated", "submit_ready": False,
            "quality": copy.deepcopy(record.get("assessment", {}).get("quality", {"status": "not_evaluated_by_exporter"})),
            "semantic_limits": semantic_limits,
            "biomed_companion": copy.deepcopy(record)}


def export_bundle(bundle, *, dictionary=None, mappings=None, gene_standard=None):
    """Create an immutable, reproducible package without imports or model calls."""
    if bundle.get("schema_version") != "knowledge-candidates-v1":
        raise ValueError("Expected a knowledge-candidates-v1 bundle")
    _dictionary(dictionary)
    mappings = mappings or {}
    records = bundle.get("records", [])
    ids = [row["group_id"] for row in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate group_id in source bundle")
    if set(mappings) - set(ids):
        raise ValueError("Mapping refers to an unknown group_id")
    outputs = [export_record(row, source_run=bundle["source_run"], dictionary=dictionary,
                             mapping=mappings.get(row["group_id"]), gene_standard=gene_standard) for row in records]
    counts = Counter(row["compatibility_status"] for row in outputs)
    operations = operation_index([operation for row in outputs for operation in row["entity_operations"]])
    return {"schema_version": SCHEMA, "source_run": bundle["source_run"],
            "target_readiness": readiness_summary(records, {r["group_id"]: r["compatibility_status"] for r in outputs}),
            "source_bundle_sha256": digest(bundle), "contract": CONTRACT,
            "target_dictionary": {"sha256": digest(dictionary), "source": dictionary.get("source", {})} if dictionary else None,
            "mapping_sha256": digest(mappings), "counts": dict(counts), "records": outputs,
            "offline_ready_count": sum(row["offline_ready"] for row in outputs),
            "entity_operations": list(operations.values()),
            "ready_after_entity_sync_count": sum(row["entity_sync_ready"] for row in outputs),
            "preview_only": (dictionary or {}).get("source", {}).get("environment") == "local_preview",
            "preview_ready_count": sum(row["preview_ready"] for row in outputs),
            "submit_ready": False, "target_contacted": False, "target_written": False}
