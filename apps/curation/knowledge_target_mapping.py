"""Conservative, source-anchored mappings for the offline evidence export.

This module reads supplied values only. It does not query a database, call a
model, contact a target service, create entities, or rewrite source knowledge.
"""

import copy
import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter, defaultdict

from apps.curation.knowledge_export import (
    _dictionary,
    _roles,
    _target_terms,
    digest,
    export_bundle,
)
from apps.curation.qualified_disease_names import negative_before, source_definitions
from apps.curation.target_entity_sync import drug_operation

REGISTRY_SCHEMA = "biomed.entity-registry.snapshot.v1"
REPORT_SCHEMA = "biomed.target-mapping-report.v1"
METHOD_VERSION = "biomed.knowledge-target-mapping.v6"
GENE_STANDARD_SCHEMA = "biomed.gene-symbols.snapshot.v1"
VARIANT = re.compile(r"(?<![\w])(?:p\.)?([ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*])(?![\w])", re.IGNORECASE)


def load_gene_standard(path):
    """Read exact official local NCBI/HGNC protein-coding symbols, never aliases."""
    hasher, symbols = hashlib.sha256(), {}
    with open(path, "rb") as source:
        for line in source:
            hasher.update(line)
            row = json.loads(line)
            symbol = row.get("symbol")
            if row.get("type") != "protein-coding" or not row.get("hgnc_id") or not isinstance(symbol, str) or not symbol:
                continue
            normalized = symbol.upper()
            if normalized in symbols:
                raise ValueError(f"Duplicate official gene symbol: {symbol}")
            symbols[normalized] = {"symbol": symbol, "gene_id": str(row["gene_id"]), "hgnc_id": str(row["hgnc_id"])}
    if not symbols:
        raise ValueError("Local gene standard contains no official protein-coding symbols")
    return {"schema_version": GENE_STANDARD_SCHEMA,
            "source": {"path": str(path), "sha256": hasher.hexdigest(), "matching": "exact_official_symbol_only"},
            "symbols": symbols}


def _key(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _target_name_key(value):
    # Preserve complete names and qualifiers; only unify typographic hyphens.
    return _key(str(value or "").translate(str.maketrans("‐‑‒–—", "-----")))


def _disease_name_key(value):
    # The source's "non–small-cell" and the registry's "non-small cell"
    # are typographic forms of the same complete disease phrase.
    return re.sub(r"\bnon[- ]small[- ]cell\b", "non-small cell", _target_name_key(value))


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return ""


def _contains(text, term):
    return bool(term) and bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", str(text), re.IGNORECASE))


def _contains_gene(text, gene):
    return bool(gene) and bool(re.search(rf"(?<!\w){re.escape(gene)}(?=p\.[A-Za-z]|\b)", str(text), re.IGNORECASE))


def _terms(row):
    return [row.get(k, "") for k in ("name", "name_cn", "name_en", "name_en_abbr")] + row.get("aliases", [])


def _name_index(dictionary):
    index = {}
    for kind, source in (("drug", "drugs"), ("disease", "tumors")):
        by_name = defaultdict(set)
        for row in dictionary.get(source, []):
            for name in _terms(row):
                normalized = _disease_name_key(name) if kind == "disease" else _target_name_key(name)
                if normalized:
                    by_name[normalized].add(_uuid(row["id"]))
        index[kind] = by_name
    return index


def _registry_index(registry):
    if registry is None:
        return {}, {}
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise ValueError(f"Expected {REGISTRY_SCHEMA}")
    if not isinstance(registry.get("source"), dict):
        raise TypeError("Registry snapshot must identify its source")
    entities, rules = {}, defaultdict(list)
    for entity in registry.get("entities", []):
        identifier = _uuid(entity.get("id"))
        if not identifier or identifier in entities:
            raise ValueError("Registry has an invalid or duplicate local entity UUID")
        entities[identifier] = entity
    for rule in registry.get("rules", []):
        scope = rule.get("scope") or {}
        scope_type = scope.get("type")
        if rule.get("kind") not in {"drug", "disease"} or scope_type not in {"global", "article"} or not _key(rule.get("alias")):
            raise ValueError("Invalid registry rule identity or scope")
        scope_key = _uuid(scope.get("key")) if scope_type == "article" else ""
        if scope_type == "article" and not scope_key:
            raise ValueError("Article-scoped rule requires an article version UUID")
        rules[(rule["kind"], _key(rule["alias"]), scope_type, scope_key)].append(rule)
    return entities, rules


def _evidence(record):
    return [a for a in record.get("anchors", []) if a.get("purpose") == "evidence" and a.get("passage_id") and isinstance(a.get("quote"), str) and a["quote"]]


def _identity_anchors(record):
    # Context can establish a name's identity, but cannot become an outcome or
    # treatment trigger; require a complete, exact source span either way.
    return [a for a in record.get("anchors", []) if a.get("purpose") in {"evidence", "context"}
            and _uuid(a.get("passage_id")) and isinstance(a.get("quote"), str) and a["quote"]
            and type(a.get("start")) is int and type(a.get("end")) is int and a["start"] >= 0
            and a["end"] - a["start"] == len(a["quote"])]


def _support(record, terms=()):
    for anchor in _evidence(record):
        if not terms or all(_contains(anchor["quote"], term) for term in terms):
            return [{"passage_id": anchor["passage_id"], "quote": anchor["quote"]}]
    return []


def _nonnegated_name_support(record, label):
    found = []
    for anchor in _identity_anchors(record):
        for match in re.finditer(re.escape(label), anchor["quote"], re.IGNORECASE):
            if negative_before(anchor["quote"], match.start()):
                return []
            found.append({"passage_id": anchor["passage_id"], "quote": anchor["quote"], "purpose": anchor["purpose"]})
    return found[:1]


def _identity(record, entity, name_index, indexed_target, entities, rules, *, allow_qualified=True):
    kind, label = entity["kind"], entity["label"]
    row = {"entity_ref": entity["ref"], "kind": kind, "label": label,
           "target_id": None, "status": "unresolved", "source_support": [
               {"passage_id": a["passage_id"], "quote": a["quote"], "purpose": a["purpose"]}
               for a in _identity_anchors(record) if _contains(a["quote"], label)][:1]}
    article_key = _uuid(record.get("version_id"))
    rule_names = {_key(label), _target_name_key(label)}
    if kind == "disease":
        rule_names.add(_disease_name_key(label))
    scoped = [r for name in rule_names for r in rules.get((kind, name, "article", article_key), [])]
    selected = scoped or [r for name in rule_names for r in rules.get((kind, name, "global", ""), [])]
    if selected:
        if (len(selected) == 1 and selected[0].get("status") == "confirmed"
                and selected[0].get("action") == "ignore"):
            rule = selected[0]
            row.update(status="not_target_entity", local_rule_id=rule.get("id"),
                       normalized_kind=(rule.get("provenance") or {}).get("normalized_kind"),
                       detail=rule.get("reason") or "Confirmed source classification excludes this drug/disease identity slot")
            return row
        if len(selected) != 1 or selected[0].get("status") != "confirmed" or selected[0].get("action") not in {"match", "alias", "new"}:
            row.update(status="local_rule_unresolved", detail="Applicable scoped/local rule is not a unique confirmed identity")
            return row
        rule = selected[0]
        canonical = entities.get(_uuid(rule.get("canonical_id")))
        if not canonical or canonical.get("status") != "active" or canonical.get("kind") != kind:
            row.update(status="local_entity_unavailable", detail="Confirmed rule has no active local entity of the same kind")
            return row
        provenance = rule.get("provenance") or {}
        qualifiers = provenance.get("required_qualifiers", [])
        if not isinstance(qualifiers, list) or any(not isinstance(q, str) or not q.strip() for q in qualifiers):
            row.update(status="local_rule_unresolved", detail="Invalid required qualifiers")
            return row
        if qualifiers and not all(any(_contains(a["quote"], q) for a in _identity_anchors(record)) for q in qualifiers):
            row.update(status="qualifier_source_missing", detail="The local rule's required qualifiers are not present in source evidence")
            return row
        if kind == "disease" and canonical.get("level") == "subtype" and canonical.get("main_id"):
            parent = entities.get(_uuid(canonical["main_id"]))
            if not parent or parent.get("kind") != "disease" or parent.get("status") != "active" or parent.get("level") != "main":
                row.update(status="main_cancer_unavailable", detail="Confirmed subtype's active main cancer was not included")
                return row
            canonical = parent
        target_kind = "drugs" if kind == "drug" else "tumors"
        provenance = canonical.get("provenance") or {}
        level = provenance.get("entity_level") or (provenance.get("local_standard") or {}).get("entity_level")
        aliases = []
        if kind == "drug" and level in {"drug_ingredient", "molecule"}:
            aliases = [values[0]["alias"] for (rule_kind, _, scope_type, _), values in rules.items()
                       if rule_kind == "drug" and scope_type == "global" and len(values) == 1
                       and values[0].get("status") == "confirmed"
                       and values[0].get("action") in {"match", "alias", "new"}
                       and not (values[0].get("provenance") or {}).get("required_qualifiers")
                       and _uuid(values[0].get("canonical_id")) == _uuid(canonical["id"])]
        external = (canonical.get("external_ids") or {}).get("tumor_api")
        row.update(local_rule_id=rule.get("id"), local_entity_id=canonical.get("id"),
                   local_entity_name=canonical.get("name"), required_qualifiers=qualifiers)
        if external:
            target = _uuid(external)
            if target not in indexed_target[target_kind]:
                row.update(status="target_uuid_not_in_snapshot", detail="Local external target UUID is not in the supplied frozen snapshot")
                return row
            row.update(target_id=target, status="confirmed_local_target_id", detail="Confirmed local rule and active entity; UUID checked against snapshot")
            return row
        matches = set()
        for name in (canonical.get("name", ""), canonical.get("name_en", ""), *aliases):
            name_key = _disease_name_key(name) if kind == "disease" else _target_name_key(name)
            matches.update(name_index[kind].get(name_key, set()))
        if len(matches) == 1:
            row.update(target_id=next(iter(matches)), status="confirmed_local_unique_target_name", detail="Confirmed local identity; its complete name uniquely matches the snapshot")
        elif len(matches) > 1:
            row.update(status="target_name_ambiguous", detail="Confirmed local identity has multiple snapshot name matches")
        else:
            row["local_entity_level"] = level
            if kind == "drug" and level in {"drug_ingredient", "molecule"}:
                if not row["source_support"]:
                    row.update(status="target_addition_invalid", detail="Automatic addition needs an exact source mention")
                    return row
                try:
                    operation = drug_operation({**canonical, "confirmed_global_aliases": aliases})
                except (KeyError, ValueError, TypeError) as error:
                    row.update(status="target_addition_invalid", detail=str(error))
                    return row
                row.update(status="automatic_target_upsert", entity_operation=operation,
                           detail="Confirmed local drug: automatically reuse or create the target entity, then bind its returned UUID")
                return row
            elif kind == "drug" and level == "drug_class":
                status = "target_class_identity_missing"
            else:
                status = "local_identity_target_missing"
            row.update(status=status, detail="Confirmed local identity has no unique target entry in this snapshot; local and ontology IDs are not target UUIDs")
        return row
    name_key = _disease_name_key(label) if kind == "disease" else _target_name_key(label)
    matches = name_index[kind].get(name_key, set())
    if len(matches) == 1:
        row.update(target_id=next(iter(matches)), status="unique_snapshot_name", detail="Complete source name uniquely matches snapshot name or alias")
    elif len(matches) > 1:
        row.update(status="target_name_ambiguous", detail="Multiple snapshot UUIDs share this complete name")
    else:
        row.update(status="target_identity_missing", detail="No unique identity in snapshot; absence is not proof of target-library absence")
        # A source-explicit stage qualifier does not change the disease's main
        # identity. The original full label remains in the record and support.
        # A local conflict on either the full label or base/abbreviation blocks
        # this projection; parenthetical text must independently identify the
        # same unique target, rather than being assumed to be an alias.
        qualified = re.fullmatch(r"(locally\s+advanced\s+or\s+metastatic|locally\s+advanced|advanced|metastatic|recurrent|early[- ]stage)\s+(.+)", label, re.IGNORECASE)
        safe_support = _nonnegated_name_support(record, label) if kind == "disease" and qualified else []
        if kind == "disease" and allow_qualified and qualified and safe_support:
            qualifier, full_base = qualified.groups()
            parenthetical = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", full_base)
            source_names = [parenthetical.group(1), parenthetical.group(2)] if parenthetical else [full_base]
            mapped = [_identity(record, {"ref": entity["ref"], "kind": kind, "label": name.strip()},
                                name_index, indexed_target, entities, rules, allow_qualified=False)
                      for name in source_names]
            if all(item["target_id"] for item in mapped) and len({item["target_id"] for item in mapped}) == 1:
                row.update(target_id=mapped[0]["target_id"], status="source_explicit_qualified_target_name", source_support=safe_support,
                           source_qualifier=qualifier, source_base_names=source_names,
                           detail="Source-explicit stage qualifier retained; complete underlying disease name and any parenthetical alias resolve to the same target identity")
    return row


def _source_defined_alias(record, row, names, indexed_target, entities, rules):
    """Resolve only this article's explicit stage-qualified parenthetical form."""
    if (row["kind"] != "disease" or row["status"] != "target_identity_missing"
            or row.get("local_rule_id") or not _uuid(record.get("version_id"))):
        return row
    definitions = source_definitions(_identity_anchors(record), row["label"])
    if not definitions:
        return row
    resolved = [_identity(record, {"ref": row["entity_ref"], "kind": "disease", "label": item["full_name"]},
                          names, indexed_target, entities, rules) for item in definitions]
    if not all(item["target_id"] for item in resolved) or len({item["target_id"] for item in resolved}) != 1:
        return row
    qualifiers = {item["qualifier"].casefold() for item in definitions}
    if len(qualifiers) != 1:
        return row
    row.update(target_id=resolved[0]["target_id"], status="source_defined_qualified_abbreviation",
               source_qualifier=definitions[0]["qualifier"], source_base_names=[item["base"] for item in definitions],
               definition_scope={"type": "article", "version_id": _uuid(record["version_id"])},
               source_support=[{"passage_id": item["passage_id"], "quote": item["quote"], "purpose": item["purpose"]} for item in definitions],
               detail="The original article explicitly defines this abbreviation using a uniquely resolved qualified cancer name; scope and qualifier are retained")
    return row


def _sentences(record):
    for anchor in _evidence(record):
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", anchor["quote"]):
            if sentence.strip():
                yield anchor, sentence.strip()


def _source_treatment(record, disease, drug, gene, variant, treatment_text):
    """Recognize only explicit single-drug receipt by variant-selected patients."""
    disease_re = re.escape(disease).replace(r"\ ", r"\s+")
    drug_re = re.escape(drug).replace(r"\ ", r"\s+")
    gene_re = re.escape(gene)
    variant_re = rf"(?:p\.)?{re.escape(variant)}"
    variant_position = rf"{gene_re}(?:\s+{variant_re}|p\.{re.escape(variant)})"
    patient = r"(?:patients?|participants?|adults?)"
    cancer = rf"{disease_re}"
    dash = r"[-‐‑‒–—]"
    context = (rf"(?:{cancer}\s+(?:harboring|carrying|bearing|with)\s+(?:an?\s+)?{variant_position}(?:\s+(?:mutation|variant))?"
               rf"|{variant_position}{dash}(?:mutant|mutated)\s+(?:advanced\s+)?{cancer})")
    population = rf"{patient}\s+with\s+(?:advanced\s*,?\s+)?{context}"
    treated = r"(?:(?:were|was|are|is)\s+(?:treated\s+with|given)|received|who\s+were\s+receiving)"
    dose = r"(?:,?\s+(?:(?:administered|given)\s+)?(?:orally\s+)?(?:at\s+a\s+dose\s+of\s+)?\d+(?:\.\d+)?\s*(?:mg|mcg|g)(?:/kg)?(?:\s+(?:orally|once|twice|daily|weekly|per\s+day)){0,4})?"
    suffix = rf"{drug_re}{dose}(?:\s+(?:treatment|monotherapy))?(?:\s+in\s+a\s+real{dash}world\s+clinical\s+setting)?\s*(?:[.!?]|$)"
    patterns = [rf"\b{population}\s+{treated}\s+(?:daily\s+)?{suffix}",
                rf"\b{drug_re}\s+(?:was|is)\s+(?:administered|given)\s+to\s+{population}\s*(?:[.!?]|$)"]
    negative = re.compile(r"\b(?:without|negative|wild[- ]type|absence|lacking|prior|previous(?:ly)?|after|before|followed|versus)\b", re.IGNORECASE)
    for anchor, sentence in _sentences(record):
        if not negative.search(sentence) and any(re.search(p, sentence, re.IGNORECASE) for p in patterns):
            return [{"passage_id": anchor["passage_id"], "quote": sentence}]
        # A phase-2 study sentence explicitly states the drug, its dose and
        # the previously treated variant-selected population. The prior
        # treatment is standard therapy, not prior receipt of this drug.
        trial = (rf"^in\s+a\s+single{dash}group,\s+phase\s+2\s+trial,\s+we\s+investigated\s+the\s+activity\s+of\s+"
                 rf"(?P<treatment>{drug_re},\s+administered\s+orally\s+at\s+a\s+dose\s+of\s+\d+(?:\.\d+)?\s+mg\s+once\s+daily),\s+"
                 rf"in\s+patients\s+with\s+{variant_position}{dash}mutated\s+advanced\s+{cancer}\s+"
                 r"previously\s+treated\s+with\s+standard\s+therapies\s*[.!?]?$")
        matched = re.search(trial, sentence, re.IGNORECASE)
        if matched and (_key(treatment_text) in {_key(drug), _key(drug + " monotherapy")}
                        or _key(matched.group("treatment")) == _key(treatment_text)):
            return [{"passage_id": anchor["passage_id"], "quote": sentence}]
    # Explicit enrollment and receipt can be adjacent sentences in the SAME
    # paragraph. Never join independently mentioned groups across paragraphs.
    paired = (rf"\beligible\s+{patient}\s+had\s+{context}\s*\.\s*"
              rf"(?:all|these)\s+(?:enrolled\s+)?{patient}\s+{treated}\s+(?:daily\s+)?{suffix}")
    for anchor in _evidence(record):
        if re.search(paired, anchor["quote"], re.IGNORECASE):
            return [{"passage_id": anchor["passage_id"], "quote": anchor["quote"]}]
        # Structured inclusion criteria bind disease, genotype and receipt to
        # the same enrolled cohort. Preserve other criteria in the companion.
        first = next(iter(re.split(r"(?<=[.!?])\s+(?=[A-Z])", anchor["quote"])), "")
        if not re.match(r"(?:patients|participants)\s+were\s+eligible\s+if\b", first, re.IGNORECASE):
            continue
        disease_names = [disease]
        for source in _identity_anchors(record):
            disease_names += re.findall(rf"(?<!\w){disease_re}\s*\(([A-Z][A-Z0-9]{{1,14}})\)", source["quote"])
        variant_clause = rf"\b(?:a\s+)?{variant_position}\s+mutation\b"
        receipt = rf"(?:^|;)\s*(?:had\s+)?received\s+{drug_re}{dose}\s*(?:until\s+[^;]+)?(?:;|\.|$)"
        if (any(_contains(first, name) for name in disease_names)
                and re.search(variant_clause, first)
                and {m.group(1).upper() for m in VARIANT.finditer(first)} == {variant}
                and not re.search(r"\b(?:ctdna|vaf|expression|pd[- ]?l1|tmb|fusion|amplification|wild[- ]type)\b", first, re.IGNORECASE)
                and not re.search(r"\b(?:without|no|absence\s+of)\s+(?:a\s+)?" + variant_position, first, re.IGNORECASE)
                and re.search(receipt, first, re.IGNORECASE)):
            return [{"passage_id": anchor["passage_id"], "quote": first}]
    return []


def _single_drug_phrase(value, drug):
    """Ignore only dosage/administration words, never a second treatment."""
    if _key(value) in {_key(drug), _key(drug + " monotherapy")}:
        return True
    return bool(re.fullmatch(
        rf"{re.escape(drug)},?\s+(?:(?:administered|given)\s+)?(?:orally\s+)?"
        r"(?:at\s+a\s+dose\s+of\s+)?\d+(?:\.\d+)?\s*(?:mg|mcg|g)(?:/kg)?"
        r"(?:\s+(?:orally|once|twice|daily|weekly|per\s+day)){0,4}", value, re.IGNORECASE))


def _single_disease_label(record, disease_refs, entities, identities):
    if len(disease_refs) == 1:
        return entities[disease_refs[0]]["label"]
    if len(disease_refs) != 2:
        return None
    by_ref = {item["entity_ref"]: item for item in identities}
    target_ids = [by_ref.get(ref, {}).get("target_id") for ref in disease_refs]
    if not all(target_ids) or len(set(target_ids)) != 1:
        return None
    names = sorted((entities[ref]["label"] for ref in disease_refs), key=len, reverse=True)
    full, abbreviation = names
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,14}", abbreviation):
        return None
    definition = re.compile(rf"(?<!\w){re.escape(full)}\s*\(\s*{re.escape(abbreviation)}\s*\)", re.IGNORECASE)
    for anchor in _identity_anchors(record):
        for match in definition.finditer(anchor["quote"]):
            if not negative_before(anchor["quote"], match.start()):
                population = str(record.get("fact", {}).get("population") or "")
                if _contains(population, abbreviation):
                    return abbreviation
    return None


def _supported_clinical_condition(record, value):
    """Allow only source-supported prior therapy or response-assessment facts."""
    if not isinstance(value, str):
        return False
    if re.search(r"\b(?:mutation|mutations|mutant|mutated|expression|positive|negative|wild[- ]type|ctdna|vaf|biomarker|co[- ]occurring|alteration|alterations)\b", value, re.IGNORECASE):
        return False
    direct = (re.fullmatch(r"previously\s+treated\s+with\s+standard\s+therapies", value, re.IGNORECASE)
              or re.fullmatch(r"\d+\s+patients\s+had\s+measurable\s+disease\s+at\s+baseline\s+and\s+were\s+evaluated\s+for\s+response", value, re.IGNORECASE))
    if direct:
        return bool(_support(record, [value]))
    history = re.fullmatch(r"(?P<percentage>\d+(?:\.\d+)?%)\s+had\s+previously\s+received\s+(?P<regimen>.+)", value, re.IGNORECASE)
    if history:
        for _, sentence in _sentences(record):
            if (_contains(sentence, history.group("percentage"))
                    and _key("had previously received " + history.group("regimen")) in _key(sentence)):
                return True
    # These describe study design/endpoint collection, not an extra trigger.
    # Exact source grounding is still required and the text is NOT dropped.
    if re.search(r"\b(?:trial|study|primary end point|primary endpoint|central review|response assessment)\b", value, re.IGNORECASE):
        return bool(_support(record, [value]))
    return False


def _simple_molecular_condition(value, gene, variant):
    if not isinstance(value, str):
        return False
    gene_pattern, variant_pattern = re.escape(gene), re.escape(variant)
    position = rf"{gene_pattern}(?:\s+(?:p\.)?|p\.){variant_pattern}"
    return bool(re.fullmatch(rf"\s*{position}(?:\s*[-‐‑‒–—]?\s*(?:mutant|mutated|mutation|variant))?\s*", value, re.IGNORECASE))


def _molecular_population_condition(record, value, gene, variant):
    """A variant-qualified disease is the same condition, not a new trigger."""
    if not isinstance(value, str) or not _support(record, [value]):
        return False
    roles = _roles(record)
    diseases = [e["label"] for e in record.get("entities", [])
                if e.get("kind") == "disease" and "disease" in roles.get(e["ref"], set())]
    position = rf"{re.escape(gene)}\s*(?:p\.)?{re.escape(variant)}"
    return any(re.fullmatch(
        rf"{position}\s*[-‐‑‒–—]\s*(?:mutant|mutated)\s+"
        rf"(?:(?:advanced|metastatic|recurrent|unresectable)\s+)*{re.escape(disease)}", value, re.IGNORECASE)
        for disease in diseases)


def _disease_condition(record, value):
    """A repeated, source-located disease is not an additional biomarker."""
    roles = _roles(record)
    return any(_disease_name_key(value) == _disease_name_key(e.get("label"))
               and any(_disease_name_key(e["label"]) in _disease_name_key(a["quote"]) for a in _identity_anchors(record))
               for e in record.get("entities", []) if e.get("kind") == "disease"
               and "disease" in roles.get(e["ref"], set()))


def _additional_result_molecular_scope(fact, gene, variant):
    """Do not flatten an outcome's biomarker subgroup into a single variant."""
    results = fact.get("results") or []
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        return True
    text = " ".join(str(item.get(field) or "") for item in results for field in ("group", "measure"))
    if re.search(r"\b(?:ctdna|vaf|pd[- ]?l1|tmb|expression|positive|negative|wild[- ]type|co[- ]occurring|tumou?r\s+mutational\s+burden|fusion|amplification)\b", text, re.IGNORECASE):
        return True
    if any(match.group(1).upper() != variant for match in VARIANT.finditer(text)):
        return True
    return any(match.group(1).upper() != gene for match in re.finditer(
        r"(?<![\w-])([A-Z][A-Z0-9-]{1,19})\s+(?:mutations?|mutated|mutant|alterations?|altered)\b", text))


def _variant_candidates(record, molecular_refs):
    entities = {e["ref"]: e for e in record.get("entities", [])}
    fact = record.get("fact", {})
    text = " ".join(json.dumps(fact.get(field) or "", ensure_ascii=False) for field in ("population", "conditions"))
    variants = {m.group(1).upper() for m in VARIANT.finditer(text)}
    result = []
    for ref in molecular_refs:
        label = entities[ref].get("label", "")
        for word in re.findall(r"(?<![\w-])[A-Z][A-Z0-9-]{1,19}(?=p\.[A-Z]|(?![\w-]))", label):
            if VARIANT.fullmatch(word) or not _contains_gene(text, word):
                continue
            for variant in variants:
                refs = [r for r in molecular_refs if _contains_gene(entities[r].get("label", ""), word)
                        or _contains(entities[r].get("label", ""), variant)]
                if any(_contains(entities[r].get("label", ""), variant) for r in refs):
                    result.append((word.upper(), variant, tuple(refs)))
    return sorted(set(result))


def _scopes(record, dictionary, gene_standard, identities):
    fact, roles = record.get("fact", {}), _roles(record)
    entities = {e["ref"]: e for e in record.get("entities", [])}
    molecular = [r for r in roles if r in entities and "molecular" in roles[r]]
    drugs = [r for r, e in entities.items() if e.get("kind") == "drug" and "intervention" in roles.get(r, set())]
    diseases = [r for r, e in entities.items() if e.get("kind") == "disease" and "disease" in roles.get(r, set())]
    evidence = _support(record)
    trigger = {"kind": "unresolved", "condition_entity_refs": [], "non_trigger_entity_refs": [],
               "logic": "unspecified", "source_support": evidence,
               "reason": "Source-anchored necessary molecular eligibility has not been established."}
    treatment = {"kind": "unspecified", "entity_refs": drugs, "groups": [], "source_support": evidence,
                 "reason": "Treatment identity, combinations and sequence require source-supported interpretation."}
    gaps, bindings = [], []
    condition_text = json.dumps({field: fact.get(field) for field in ("conditions", "results")}, ensure_ascii=False)
    if (re.search(r"\b(?:ctDNA|circulating\s+tumou?r\s+DNA|variant\s+allele\s+fraction|VAF)\b", condition_text, re.IGNORECASE)
            and re.search(r"\b(?:clearance|cleared|detectable|undetectable|dynamic|increase|decrease|low\s+concentration|level|levels)\b", condition_text, re.IGNORECASE)):
        gaps.append("dynamic_ctdna_or_vaf_requires_review")
    treatment_text = str(fact.get("treatment") or "").strip()
    # Distinguish only explicitly transcribed treatment logic; never flatten it.
    treatment_support = _support(record, [treatment_text]) if treatment_text else []
    if treatment_support:
        if re.search(r"\b(?:followed by|sequential(?:ly)?|then)\b", treatment_text, re.IGNORECASE):
            treatment.update(kind="sequential", source_support=treatment_support)
        elif re.search(r"\b(?:or|versus|vs\.?)\b", treatment_text, re.IGNORECASE):
            treatment.update(kind="alternatives", source_support=treatment_support)
        elif re.search(r"\b(?:plus|combined with|in combination with|and)\b|\+", treatment_text, re.IGNORECASE):
            treatment.update(kind="combination", source_support=treatment_support)
    candidates = _variant_candidates(record, molecular)
    if len(candidates) != 1:
        gaps.append("specific_gene_variant_condition_not_unique")
        context = " ".join(json.dumps(fact.get(field) or "", ensure_ascii=False) for field in ("population", "conditions"))
        if re.search(r"\b(?:expression|protein|serum|signature|score)\b", context, re.IGNORECASE):
            gaps.append("expression_or_non_dna_marker_requires_review")
        if re.search(r"\b(?:mutations?|mutated|mutants?|alterations?)\b", context, re.IGNORECASE):
            gaps.append("variant_not_identified_or_ambiguous")
        if re.search(r"\bfusions?\b", context, re.IGNORECASE):
            gaps.append("fusion_scope_requires_review")
        return trigger, treatment, bindings, gaps
    gene, variant, refs = candidates[0]
    trigger["condition_entity_refs"] = list(refs)
    if gene_standard is not None:
        gene_record = gene_standard["symbols"].get(gene)
        gene_basis = "exact official local NCBI/HGNC protein-coding symbol"
    else:
        # Only already-recorded target genes can be recognized without the
        # local standard; a target key itself remains independently checked.
        known = {str(row.get("gene_symbol", "")).upper() for row in dictionary.get("logical_targets", [])}
        gene_record = {"symbol": gene} if gene in known else None
        gene_basis = "exact symbol in the frozen target logical keys"
    if not gene_record:
        gaps.append("gene_symbol_not_confirmed_in_local_standard")
        trigger["reason"] += " Family names and unconfirmed gene names cannot be used as target gene symbols."
        return trigger, treatment, bindings, gaps
    if set(refs) != set(molecular):
        gaps.append("additional_molecular_roles_need_classification")
    if "dynamic_ctdna_or_vaf_requires_review" in gaps:
        return trigger, treatment, bindings, gaps
    if _additional_result_molecular_scope(fact, gene, variant):
        gaps.append("additional_result_molecular_scope_requires_review")
        return trigger, treatment, bindings, gaps
    logic = _key(fact.get("molecular_logic"))
    if logic not in {"", "single"}:
        gaps.append("compound_condition_logic_requires_review")
        return trigger, treatment, bindings, gaps
    conditions = fact.get("conditions") or []
    if isinstance(conditions, str):
        conditions = [conditions]
    clinical_context = [value for value in conditions if _supported_clinical_condition(record, value)] if isinstance(conditions, list) else []
    if not isinstance(conditions, list) or any(
        not _supported_clinical_condition(record, value) and not _simple_molecular_condition(value, gene, variant)
        and not _molecular_population_condition(record, value, gene, variant)
        and not _disease_condition(record, value)
        for value in conditions
    ):
        gaps.append("additional_fact_conditions_require_review")
        return trigger, treatment, bindings, gaps
    fact_scope = " ".join(json.dumps(fact.get(field) or "", ensure_ascii=False) for field in ("population", "conditions"))
    if ({m.group(1).upper() for m in VARIANT.finditer(fact_scope)} != {variant}
            or re.search(r"\b(?:and|or|co[- ]occurring|concurrent)\b", str(fact.get("population") or ""), re.IGNORECASE)):
        gaps.append("additional_fact_conditions_require_review")
        return trigger, treatment, bindings, gaps
    execution = record.get("assessment", {}).get("execution", {}).get("status")
    if execution and execution != "complete":
        gaps.append("source_extraction_incomplete")
        return trigger, treatment, bindings, gaps
    disease = _single_disease_label(record, diseases, entities, identities)
    if fact.get("relation") != "treatment_outcome" or len(drugs) != 1 or not disease:
        gaps.append("single_drug_disease_treatment_relation_not_established")
        return trigger, treatment, bindings, gaps
    drug = entities[drugs[0]]["label"]
    dose_description = _single_drug_phrase(treatment_text, drug) and _key(treatment_text) != _key(drug)
    if not _single_drug_phrase(treatment_text, drug):
        gaps.append("single_drug_treatment_not_explicit_in_fact")
        return trigger, treatment, bindings, gaps
    support = _source_treatment(record, disease, drug, gene, variant, treatment_text)
    if not support:
        gaps.append("source_does_not_prove_variant_selected_treatment")
        return trigger, treatment, bindings, gaps
    treatment.update(kind="single", groups=[{"entity_refs": drugs, "label": treatment_text}], source_support=support,
                     reason="Exact source sentence identifies one drug received by the selected patients.")
    trigger.update(kind="direct", logic="single", source_support=support,
                   reason="The stored population/conditions and source sentence identify a specific gene and variant in the treated population.")
    if clinical_context:
        trigger["unstructured_clinical_context"] = copy.deepcopy(clinical_context)
        trigger["clinical_context_enforced_by_target"] = False
    if dose_description:
        treatment["dose_enforced_by_target"] = False
    matches = [row for row in dictionary.get("logical_targets", []) if str(row.get("event_type", "")).lower() == "mutation"
               and str(row.get("gene_symbol", "")).upper() == gene and variant.casefold() in _target_terms("mutation", row.get("target_key", ""))]
    if not matches:
        gaps.append("snapshot_not_covering_key")
        trigger["reason"] += " The partial snapshot has no matching logical key; current target existence is unknown."
    bindings.append({"entity_refs": list(refs), "basis": f"{gene_basis}; exact variant and gene in stored population/conditions and source; snapshot coverage is reported separately.",
                     "gene_standard_identity": copy.deepcopy(gene_record),
                     "gene_standard_source": copy.deepcopy(gene_standard["source"]) if gene_standard is not None else None,
                     "source_support": support,
                     "input": {"trigger_type": "direct", "event_type": "mutation", "gene_symbol": gene, "target_key": variant}})
    return trigger, treatment, bindings, gaps


def prepare_mappings(bundle, dictionary, registry=None, *, gene_standard=None):
    """Return export_bundle-compatible mappings and a reproducible offline report."""
    if bundle.get("schema_version") != "knowledge-candidates-v1":
        raise ValueError("Expected a knowledge-candidates-v1 bundle")
    indexed_target = _dictionary(dictionary)
    if dictionary is None:
        raise ValueError("A frozen target dictionary is required")
    if gene_standard is not None and (gene_standard.get("schema_version") != GENE_STANDARD_SCHEMA
                                      or not isinstance(gene_standard.get("symbols"), dict)
                                      or not isinstance(gene_standard.get("source"), dict)):
        raise ValueError("Invalid local gene standard snapshot")
    entities, rules = _registry_index(registry)
    names = _name_index(dictionary)
    mappings, rows = {}, []
    for record in bundle.get("records", []):
        group = record["group_id"]
        if group in mappings:
            raise ValueError("Duplicate group_id in source bundle")
        roles = _roles(record)
        participants = [e for e in record.get("entities", []) if
                        (e.get("kind") == "drug" and "intervention" in roles.get(e["ref"], set())) or
                        (e.get("kind") == "disease" and "disease" in roles.get(e["ref"], set()))]
        identities = [_source_defined_alias(record, _identity(record, e, names, indexed_target, entities, rules),
                                            names, indexed_target, entities, rules) for e in participants]
        trigger, treatment, bindings, gaps = _scopes(record, dictionary, gene_standard, identities)
        ids = {e["entity_ref"]: e["target_id"] for e in identities if e["target_id"]}
        operations = {e["entity_operation"]["operation_id"]: e["entity_operation"]
                      for e in identities if e.get("entity_operation")}
        operation_refs = {e["entity_ref"]: e["entity_operation"]["operation_id"]
                          for e in identities if e.get("entity_operation")}
        unresolved = [e["entity_ref"] for e in identities if not e["target_id"] and e["entity_ref"] not in operation_refs]
        mappings[group] = {"entity_ids": ids, "unresolved_entity_refs": unresolved,
                           "identity_diagnostics": {e["entity_ref"]: {"status": e["status"], "detail": e.get("detail", "")}
                                                    for e in identities if e["entity_ref"] in unresolved},
                           "basis": "Frozen dictionary and, when supplied, confirmed local registry rules; see mapping report for each identity.",
                           "trigger_scope": trigger, "treatment_scope": treatment, "trigger_bindings": bindings,
                           "review_only": True}
        if operations:
            mappings[group].update(entity_operations=list(operations.values()), entity_operation_refs=operation_refs)
        rows.append({"group_id": group, "entity_mappings": identities, "gaps": gaps})
    package = export_bundle(bundle, dictionary=dictionary, mappings=mappings)
    for row, exported in zip(rows, package["records"], strict=True):
        row.update(compatibility_status=exported["compatibility_status"], offline_ready=exported["offline_ready"],
                   mapping_issues=copy.deepcopy(exported["mapping_issues"]))
    report = {"schema_version": REPORT_SCHEMA, "mapping_method_version": METHOD_VERSION, "source_run": bundle["source_run"],
              "source_bundle_sha256": digest(bundle), "dictionary_sha256": digest(dictionary),
              "dictionary_snapshot_id": dictionary["source"]["snapshot_id"],
              "registry_sha256": digest(registry) if registry is not None else None,
              "gene_standard_source": copy.deepcopy(gene_standard["source"]) if gene_standard is not None else None,
              "mapping_sha256": digest(mappings), "counts": package["counts"],
              "gap_counts": dict(Counter(gap for row in rows for gap in row["gaps"])),
              "offline_ready_count": package["offline_ready_count"], "records": rows,
              "automatic_entity_operation_count": len(package["entity_operations"]),
              "ready_after_entity_sync_count": package["ready_after_entity_sync_count"],
              "live_sample_matching": "unverified", "target_contacted": False,
              "target_written": False, "submit_ready": False}
    return mappings, report
