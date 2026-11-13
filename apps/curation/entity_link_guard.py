"""Two source-backed checks on selected links, not an identity verifier.

``retain`` means neither check objected; it does not mean verified or published.
Metadata is frozen research input, never populated from evaluation verdicts.
The caller retains the original mention and candidate when a link is unresolved.
"""

import re
import unicodedata

GUARD_VERSION = "source-scope-class-1"
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})
_SYNONYM = re.compile(r'^synonym: "((?:[^"\\]|\\.)*)" (EXACT|BROAD|NARROW|RELATED)\b')
_BASED_CLASS = re.compile(r"^(.+?)[- ]based\s+drugs?$", re.IGNORECASE)
_TREATMENT = re.compile(
    r"\b(?:chemotherapy|chemoradiotherapy|therapy|regimens?|treatment|treated|"
    r"receive[ds]?|administered|given|induction)\b"
)


def _key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).translate(_DASHES).casefold().split())


def parse_mondo_scopes(lines, identifiers=None):
    """Restore OBO synonym scopes; optionally retain a bounded concept subset.

    Pass lines from the exact source release, not a current online ontology.
    Identifier filtering only reduces artifact size and has no scoring semantics.
    """
    selected = set(identifiers) if identifiers is not None else None
    result = {}
    identifier = None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            identifier = None
        elif line.startswith("id: "):
            identifier = line[4:]
        elif identifier and (selected is None or identifier in selected):
            match = _SYNONYM.match(line)
            if match:
                # OBO quoted strings use escaped quotes/backslashes, not Python
                # unicode escapes; leave other escape sequences unchanged.
                alias = re.sub(r'\\(["\\])', r"\1", match[1])
                scopes = result.setdefault(identifier, {}).setdefault(_key(alias), [])
                if match[2] not in scopes:
                    scopes.append(match[2])
    return result


def build_guard_metadata(*, mondo_scopes=None, rxclasses=(), sources=()):
    """Build a small reusable artifact from source scopes and RxClass EPC rows.

    Only the explicit ``X-based Drug`` naming pattern yields a short class alias.
    No fuzzy class matching or chemical-element inference is attempted. Sources
    should include path/URL, version and SHA-256 of the exact input snapshots.
    """
    aliases = {}
    for row in rxclasses:
        match = _BASED_CLASS.fullmatch(row.get("name", ""))
        if match and row.get("class_type") == "EPC":
            aliases.setdefault(_key(match[1]), []).append({
                "id": row["id"], "name": row["name"], "class_type": "EPC",
                "alias_rule": "explicit_based_drug_suffix",
            })
    return {
        "schema_version": "entity-link-guard-metadata-1",
        "synonym_scopes": mondo_scopes or {},
        "class_aliases": aliases,
        "sources": list(sources),
    }


def _class_contexts(mention, packet):
    """Locate explicit class/therapy or hyphenated combination use.

    Requiring a local construction avoids flagging a bare chemical name merely
    because another sentence in the same paper discusses cancer treatment.
    """
    word = re.escape(_key(mention))
    pattern = re.compile(
        rf"(?<!\w)(?:[\w]+\s*-\s*{word}|{word}\s*-\s*based|"
        rf"{word}\s+(?:chemotherapy|regimens?|therapy|agents?|drugs?))(?!\w)"
    )
    contexts = []
    for passage in packet.get("passages", []):
        text = _key(passage.get("text", ""))
        for match in pattern.finditer(text):
            sentence_start = text.rfind(".", 0, match.start()) + 1
            sentence_end = text.find(".", match.end())
            sentence = text[sentence_start:sentence_end if sentence_end >= 0 else len(text)]
            if _TREATMENT.search(sentence):
                contexts.append({"passage_id": passage.get("id"), "construction": match[0]})
    return contexts


def guard_link(entity, candidate, packet, metadata=None):
    """Return retain/unresolved with inspectable reasons; never replace an ID.

    Checks: (1) a non-equivalent ontology alias alone cannot establish identity;
    (2) an ingredient/molecule named like an explicit source drug-class short
    name in a local treatment construction remains ambiguous. Missing metadata
    does not silently invent a scope or classify an element. Other errors pass
    through; this intentionally bounded guard is not complete disambiguation.
    """
    metadata = metadata or {}
    reasons = []
    warnings = []
    matches = candidate.get("matching_evidence", [])
    scopes = metadata.get("synonym_scopes", {}).get(candidate.get("id"), {})
    identity_match = False
    nonidentity = []
    for match in matches:
        field = match.get("dictionary_field")
        if field in {"label", "code", "identifier"}:
            identity_match = True
        elif field == "synonym":
            matched_scopes = scopes.get(_key(match.get("dictionary_text", "")), [])
            if "EXACT" in matched_scopes:
                identity_match = True
            elif matched_scopes:
                nonidentity.append({"alias": match.get("dictionary_text"), "scopes": matched_scopes})
            elif candidate.get("synonym_scope") == "not_preserved":
                warnings.append("synonym_scope_unavailable")
    if nonidentity and not identity_match:
        reasons.append({"code": "non_equivalent_synonym", "matches": nonidentity})

    aliases = metadata.get("class_aliases", {}).get(_key(entity.get("text", "")), [])
    if aliases and candidate.get("entity_level") in {"drug_ingredient", "molecule"}:
        contexts = _class_contexts(entity.get("text", ""), packet)
        if contexts:
            reasons.append({"code": "ingredient_class_ambiguity", "classes": aliases, "contexts": contexts})

    return {
        "version": GUARD_VERSION,
        "decision": "unresolved" if reasons else "retain",
        "candidate_id": candidate.get("id"),
        "reasons": reasons,
        "warnings": sorted(set(warnings)),
        "sources": metadata.get("sources", []),
    }
