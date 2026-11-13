"""Bounded source-only type refinements; no identity inference or model calls."""

import re
from copy import deepcopy

VERSION = "source-entity-types-v2"
CLASS_RULE = "explicit_local_treatment_definition-v1"
VARIANT_RULE = "explicit_protein_substitution_mention-v1"
GENE_RULE = "explicit_source_gene_list-v1"
RULES = {
    CLASS_RULE: "Located drug acronym, no selected RXNORM/CHEMBL link; one unambiguous same-article/version definition with exact word initials and inhibition/blockade/therapy/treatment ending, plus local acronym therapy/treatment/regimen use.",
    VARIANT_RULE: "Located biomarker whose complete mention is an optional gene/protein-like prefix, one-letter protein substitution, and mutant/mutation suffix. No expression/signature, DNA, gene-ID or HGVS inference.",
    GENE_RULE: "Located drug-typed symbol, no selected ingredient/molecule link; exact membership in a same-version source list explicitly identified as genes. No protein-to-gene conversion or standard ID inference.",
}
_CLASS_ENDINGS = {"inhibition", "inhibitions", "blockade", "blockades", "therapy", "therapies", "treatment", "treatments"}
_AMINO = "ACDEFGHIKLMNPQRSTVWY"
_VARIANT = re.compile(
    rf"(?:(?:[A-Z][A-Z0-9]{{0,19}}|p[0-9]+)\s+)?(?:p\.)?[{_AMINO}][1-9][0-9]*[{_AMINO}]"
    r"(?:[-‐‑‒–—−]|\s)+(?:mutant|mutation)s?"
)
_EXCLUDED_VARIANT = re.compile(r"\b(?:expression|expressed|signature|comutation|not|non|wild[- ]?type)\b", re.IGNORECASE)
_SYMBOL = r"[A-Z][A-Z0-9]{1,19}"
_GENE_LIST = re.compile(
    rf"(?<!\w)(?P<symbols>{_SYMBOL}(?:(?:,\s*(?:and\s+)?|\s+and\s+){_SYMBOL})+)"
    r"\s+were identified as (?:[A-Za-z0-9][A-Za-z0-9\-‐‑–]*\s+){0,4}genes\b"
)


def identity(value):
    return tuple(value.get(key) for key in ("pmid", "version_id", "source_sha256"))


def _require(value, message):
    if not value:
        raise ValueError(message)


def _quote(passage, start, end, kind):
    text = passage["text"]
    left = max(text.rfind(mark, 0, start) for mark in (".", "!", "?", "\n")) + 1
    while left < start and text[left].isspace():
        left += 1
    stops = [text.find(mark, end) for mark in (".", "!", "?", "\n")]
    right = min((p for p in stops if p >= 0), default=len(text))
    if right < len(text) and text[right] in ".!?":
        right += 1
    return {
        "kind": kind, "passage_id": passage["id"],
        **{key: passage[key] for key in ("pmid", "version_id", "source_sha256", "parser_version", "locator")},
        "start": start, "end": end, "quote": text[start:end],
        "sentence": {"start": left, "end": right, "text": text[left:right]},
        "coordinate_space": "parsed_passage_unicode_codepoints",
    }


def _source_locations(entity, packet):
    by_id = {p["id"]: p for p in packet["passages"]}
    locations = entity.get("locations")
    _require(isinstance(locations, list), f"Missing locations: {entity.get('id')}")
    result = []
    for location in locations:
        passage = by_id.get(location.get("passage_id"))
        start, end = location.get("start"), location.get("end")
        _require(passage is not None and type(start) is int and type(end) is int
                 and 0 <= start < end <= len(passage["text"])
                 and passage["text"][start:end] == entity["text"],
                 f"Invalid original entity location: {entity.get('id')}")
        result.append((passage, start, end))
    return result


def _class_evidence(entity, packet, article_context):
    mention = entity["text"]
    if not re.fullmatch(r"[A-Z]{2,8}", mention):
        return None, "not_a_strict_acronym"
    selected = (entity.get("link") or {}).get("candidate_id")
    if isinstance(selected, str) and selected.startswith(("RXNORM:", "CHEMBL:")):
        return None, "selected_ingredient_or_molecule_link"
    local_pattern = re.compile(rf"(?<!\w){re.escape(mention)}\s+(?i:therapy|therapies|treatments?|regimens?)(?!\w)")
    local = [_quote(p, m.start(), m.end(), "local_treatment_use")
             for p in packet["passages"] for m in local_pattern.finditer(p["text"])]
    if not local:
        return None, "no_local_treatment_use"
    # Exactly N adjacent words for an N-letter acronym; no stopword or fuzzy expansion.
    word = r"[A-Za-z]+(?:-[A-Za-z]+)*"
    definition = re.compile(rf"(?<![A-Za-z0-9-])(?P<long>(?:{word}\s+){{{len(mention)-1}}}{word})\s*\({re.escape(mention)}\)")
    expansions = {}
    for passage in article_context:
        if identity(passage) != identity(packet["article"]):
            continue
        for match in definition.finditer(passage["text"]):
            words = match["long"].split()
            if "".join(w[0].upper() for w in words) != mention:
                continue
            key = " ".join(words).casefold()
            expansions.setdefault(key, []).append(_quote(passage, match.start(), match.end(), "explicit_acronym_definition"))
    if len(expansions) != 1:
        return None, "conflicting_definitions" if expansions else "no_strict_same_version_definition"
    expansion, evidence = next(iter(expansions.items()))
    if expansion.split()[-1] not in _CLASS_ENDINGS:
        return None, "definition_is_not_a_supported_treatment_class"
    return evidence + local, "explicit_source_treatment_class"


def _retype(entity, target):
    result = deepcopy(entity)
    result["type"] = target
    link = entity.get("link") or {}
    if link.get("candidate_id") or link.get("proposed_id"):
        # An old-type candidate has not been revalidated for the new type.
        # Preserve its entire history in original_entities; never invent a new ID.
        result["link"] = {**deepcopy(link), "candidate_id": None, "proposed_id": None,
                          "method": "deferred_after_source_type_change", "status": "research_candidate_not_confirmed"}
        action = "revoked_pending_type_compatible_revalidation"
    else:
        action = "unchanged_unlinked"
    result["type_refinement"] = {
        "version": VERSION, "original_type": entity["type"], "standard_link_action": action,
        "candidate_retrieval_and_guards_are_original_type_history": True,
        "new_standard_id_inferred": False,
    }
    return result, action


def _gene_evidence(entity, packet, article_context):
    mention = entity["text"]
    if not re.fullmatch(_SYMBOL, mention):
        return None, "not_a_single_symbol"
    selected = (entity.get("link") or {}).get("candidate_id")
    if isinstance(selected, str) and selected.startswith(("RXNORM:", "CHEMBL:", "std:RXNORM:", "std:CHEMBL:")):
        return None, "selected_ingredient_or_molecule_link"
    evidence = []
    for passage in article_context:
        if identity(passage) != identity(packet["article"]):
            continue
        for match in _GENE_LIST.finditer(passage["text"]):
            if mention in re.findall(_SYMBOL, match["symbols"]):
                evidence.append(_quote(passage, match.start(), match.end(), "explicit_gene_list_membership"))
    return (evidence, "explicit_source_gene_list") if evidence else (None, "no_explicit_source_gene_list")


def refine(packet, entities, article_context):
    """Return a separate typed view with unchanged mentions and original offsets."""
    article = packet["article"]
    _require(all(isinstance(v, str) and v for v in identity(article)), "Missing article source identity")
    passage_ids = [p["id"] for p in packet["passages"]]
    _require(len(set(passage_ids)) == len(passage_ids), "Duplicate original passage IDs")
    for passage in packet["passages"]:
        _require(identity(passage) == identity(article), "Packet passage source identity differs")
    ids = [e["id"] for e in entities]
    _require(len(set(ids)) == len(ids), "Duplicate original entity IDs")
    typed, changes, checked = [], [], []
    for entity in entities:
        locations = _source_locations(entity, packet)
        target, evidence, rule, reason = None, None, None, "type_outside_rule_scope"
        if not locations:
            reason = "unlocated_original_entity"
        elif entity["type"] == "drug":
            evidence, reason = _gene_evidence(entity, packet, article_context)
            if evidence:
                target, rule = "gene", GENE_RULE
            else:
                evidence, reason = _class_evidence(entity, packet, article_context)
                if evidence:
                    target, rule = "treatment", CLASS_RULE
        elif entity["type"] == "biomarker":
            if _VARIANT.fullmatch(entity["text"]) and not _EXCLUDED_VARIANT.search(entity["text"]):
                target, rule, reason = "variant", VARIANT_RULE, "explicit_protein_substitution_mention"
                evidence = [_quote(p, start, end, "original_variant_mention") for p, start, end in locations]
            else:
                reason = "not_a_complete_explicit_substitution_mention"
        status = {"entity_id": entity["id"], "original_type": entity["type"],
                  "status": "changed" if target else "untouched", "reason": reason}
        if target:
            result, action = _retype(entity, target)
            change = {"entity_id": entity["id"], "text": entity["text"], "from_type": entity["type"],
                      "to_type": target, "rule_id": rule, "evidence": evidence, "standard_link_action": action,
                      "original_link": deepcopy(entity.get("link")), "mention_and_locations_unchanged": True}
            if target == "variant":
                change["interpretation"] = "Protein substitution mention only; no DNA change, exact gene, HGVS normalization or standard ID inferred."
            elif target == "gene":
                change["interpretation"] = "Explicit source gene-list membership; source symbol retained, no gene ID or protein identity inferred."
            changes.append(change)
            status.update(rule_id=rule, resulting_type=target)
        else:
            result = deepcopy(entity)
        typed.append(result)
        checked.append(status)
    return {"id": packet["id"], "article": deepcopy(article), "original_entities": deepcopy(entities),
            "entities": typed, "changes": changes, "checked": checked,
            "full_article_context_is_model_input": False}
