"""Project source-defined cancer subtypes to a frozen target main-cancer ID.

Only an explicit article abbreviation that uniquely matches a local OncoTree
code and complete subtype name is considered. This does not change the source
subtype, write the registry, or imply that the target enforces subtype scope.
"""

import copy
import re
import unicodedata
import uuid

from apps.curation.qualified_disease_names import negative_before

VERSION = "biomed.source-disease-main-projection.v1"


def _name_key(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    words = re.findall(r"[a-z0-9]+", value)
    # Oncology standard names commonly end in Cancer while source papers use
    # Carcinoma. All preceding organ, grade and histology terms must match.
    if words and words[-1] == "carcinoma":
        words[-1] = "cancer"
    return tuple(words)


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def project_main_disease(entity, evidence, nodes, dictionary, *, prior_status, ontology_source):
    """Return one source-supported projection or None; ambiguity stays unresolved."""
    if (entity.get("kind") != "disease" or not isinstance(entity.get("label"), str)
            or prior_status not in {"local_identity_target_missing", "target_identity_missing"}
            or not isinstance(nodes, list) or not isinstance(dictionary, dict)
            or not isinstance(ontology_source, dict) or not ontology_source.get("sha256")):
        return None
    label = entity["label"].strip()
    if not label:
        return None
    marker = re.compile(rf"(?<!\w){re.escape(label)}\s*\(\s*([A-Z][A-Z0-9]{{1,14}})\s*\)", re.IGNORECASE)
    candidates = []
    for anchor in evidence.values():
        quote = anchor.get("quote", "")
        for hit in marker.finditer(quote):
            if negative_before(quote, hit.start()):
                continue
            code = hit.group(1)
            if code != code.upper():
                continue
            standards = [node for node in nodes if node.get("code") == code
                         and _name_key(node.get("en")) == _name_key(label)
                         and node.get("main_type")]
            if len(standards) != 1:
                continue
            main = standards[0]["main_type"]
            matches = {pk for row in dictionary.get("tumors", [])
                       if _name_key(main) in {_name_key(row.get(field)) for field in
                                             ("name", "name_cn", "name_en", "name_en_abbr")}
                       for pk in [_uuid(row.get("id"))] if pk}
            if len(matches) != 1:
                continue
            candidates.append({"target_id": next(iter(matches)), "main_cancer": main,
                               "source_subtype": label, "standard_code": code,
                               "standard_identifier": standards[0].get("identifier"),
                               "source_support": copy.deepcopy(anchor),
                               "ontology_source": copy.deepcopy(ontology_source)})
    identities = {(row["target_id"], row["standard_code"]) for row in candidates}
    if len(identities) != 1:
        return None
    return {"version": VERSION, **candidates[0], "target_enforces_subtype": False,
            "scope_note": "Original source subtype is retained; target ID represents the main cancer only."}
