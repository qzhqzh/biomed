"""Exact source-defined, article-scoped disease names; no identity inference."""

import re

_QUALIFIER = r"locally\s+advanced\s+or\s+metastatic"
_DEFINITION = re.compile(
    rf"(?<!\w)(?P<qualifier>{_QUALIFIER})\s+"
    r"(?P<base>[A-Za-z][A-Za-z '\-‐‑‒–—]{1,99}?)\s*$",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:no|without|absence|excluded|exclude|not)\b", re.IGNORECASE)
_COMPOUND = re.compile(r"\b(?:and|or|versus|vs|not|without)\b", re.IGNORECASE)


def negative_before(text, start):
    boundary = max(text.rfind(".", 0, start), text.rfind(";", 0, start)) + 1
    return bool(_NEGATION.search(text[boundary:start]))


def source_definitions(anchors, abbreviation):
    """Return only unambiguous, non-negated literal source definitions.

    The caller must independently resolve every full name and keep the
    resulting abbreviation scoped to this article. Offsets are relative to
    each supplied anchor quote.
    """
    if not isinstance(abbreviation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/\-]{1,29}", abbreviation):
        return []
    marker = re.compile(rf"\(\s*{re.escape(abbreviation)}\s*\)", re.IGNORECASE)
    found = []
    for anchor_index, anchor in enumerate(anchors):
        quote = anchor.get("quote")
        if anchor.get("purpose") not in {"evidence", "context"} or not isinstance(quote, str) or not anchor.get("passage_id"):
            continue
        for occurrence in marker.finditer(quote):
            before = quote[max(0, occurrence.start() - 150):occurrence.start()]
            definition = _DEFINITION.search(before)
            if not definition or _COMPOUND.search(definition.group("base")):
                return []
            start = max(0, occurrence.start() - 150) + definition.start()
            if negative_before(quote, start):
                return []
            found.append({"passage_id": anchor["passage_id"], "purpose": anchor["purpose"], "quote": quote[start:occurrence.end()],
                          "anchor_index": anchor_index,
                          "start_in_anchor": start, "end_in_anchor": occurrence.end(),
                          "full_name": f"{definition.group('qualifier')} {definition.group('base')}",
                          "qualifier": definition.group("qualifier"), "base": definition.group("base")})
    return found
