"""Split literal mutation-qualified disease mentions without inventing names."""

import re
from copy import deepcopy

VERSION = "compound-entity-mentions-v1"
QUALIFIED = re.compile(
    r"^(?P<variant>[A-Z][A-Z0-9-]{1,19}\s*(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9]\d{0,5}[ACDEFGHIKLMNPQRSTVWY*])"
    r"\s*[-‐‑‒–—]\s*(?:mutant|mutated)\s+"
    r"(?:(?:advanced|metastatic|recurrent|unresectable)\s+)*(?P<disease>.+)$"
)


def expand_inventory(packet):
    """Return a derived inventory; original model entries and offsets stay frozen."""
    entries = []
    passages = {p["id"]: p for p in packet["passages"]}
    for original in packet.get("entities", []):
        entity = deepcopy(original)
        match = QUALIFIED.fullmatch(entity["text"]) if entity["type"] == "disease" else None
        children = []
        if match:
            for kind in ("variant", "disease"):
                start, end = match.span(kind)
                locs = []
                for loc in entity.get("locations", []):
                    passage = passages.get(loc.get("passage_id"))
                    offset = loc.get("start")
                    if (passage and type(offset) is int and offset >= 0
                            and passage["text"][offset:loc.get("end")] == entity["text"]):
                        locs.append({**loc, "start": offset + start, "end": offset + end})
                if locs:
                    children.append({"id": entity["id"] + "." + kind, "type": kind,
                        "text": match.group(kind), "locations": locs,
                        "derived_from": {"ref": entity["id"], "text": entity["text"], "method": VERSION}})
        if len(children) == 2:
            entity["compound_context_only"] = True
        entries.append(entity)
        if entity.get("compound_context_only"):
            entries.extend(children)
    return entries
