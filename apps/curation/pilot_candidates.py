"""Offline, high-recall passage candidates; never reviewed clinical evidence.

Only supplied passage text is inspected. The caller retains document/version
identity and controls which source passages are available. No database, model,
network, or publication side effects are involved.
"""

import re

VERSION = "treatment-passage-rules-v1"

# Categories indicate lexical cues, not identified or normalized entities.
_CUE_PATTERNS = {
    "treatment": (
        r"\b(?:treat(?:ment|ments|ed|ing)|therap(?:y|ies|eutic)|drug(?:s)?|"
        r"chemotherap(?:y|ies)|immunotherap(?:y|ies)|radiotherap(?:y|ies)|"
        r"radiation|regimen(?:s)?|monotherapy|combination therap(?:y|ies)|"
        r"inhibitor(?:s)?|checkpoint blockade|placebo|adjuvant|neoadjuvant)\b"
    ),
    "outcome": (
        r"\b(?:survival|response(?:s)?|respond(?:ed|ers?|ing)|remission|"
        r"progression|recurrence|relapse(?:d)?|resistan(?:ce|t)|sensitiv(?:ity|e)|"
        r"benefit(?:s|ed)?|efficacy|effective(?:ness)?|toxicit(?:y|ies)|"
        r"adverse events?|outcomes?|hazard ratio|odds ratio|relative risk|"
        r"confidence interval|PFS|OS|ORR|DFS|DOR)\b"
    ),
    "biomarker": (
        r"\b(?:biomarkers?|mutations?|mutants?|variants?|fusions?|"
        r"amplification|overexpression|expression|wild[- ]type|"
        r"EGFR|HER[- ]?2|ERBB2|KRAS|NRAS|BRAF|ALK|ROS1|RET|"
        r"NTRK[123]?|BRCA[12]?|PD[- ]?L1|MSI[- ]?H|dMMR|TMB)\b"
    ),
    "negation_or_uncertainty": (
        r"\b(?:no|not|neither|without|failed|uncertain|unclear|"
        r"inconclusive|non[- ]significant|insufficient)\b"
    ),
    "comparison": (
        r"\b(?:versus|vs\.?|compared|comparison|control|placebo|"
        r"however|whereas|although|but)\b"
    ),
}
_PATTERNS = {
    category: re.compile(pattern, re.IGNORECASE)
    for category, pattern in _CUE_PATTERNS.items()
}
_RELEVANCE_CATEGORIES = {"treatment", "outcome", "biomarker"}


def _cues(text):
    matches = []
    for category, pattern in _PATTERNS.items():
        for match in pattern.finditer(text):
            matches.append(
                {
                    "category": category,
                    "start": match.start(),
                    "end": match.end(),
                    "text": match.group(),
                }
            )
    return sorted(
        matches, key=lambda item: (item["start"], item["end"], item["category"])
    )


def locate_candidates(document, max_candidates=100):
    """Locate whole-passage candidates in stable input order.

    At least two of treatment, outcome and biomarker cue categories must occur
    in the same passage. Negation is retained, not used to reject a passage.
    Offsets are Python string character offsets into the unchanged passage.
    The full supplied passage is the quote/context, including multiple sentences.

    ``completed`` only means all supplied passages were scanned. It does not
    imply complete full text, relevant evidence, or semantic validation. The
    emission budget never changes the total match count or silently drops work.
    """
    if not isinstance(document, dict):
        raise TypeError("document must be a dictionary")
    passages = document.get("passages", [])
    if not isinstance(passages, list):
        raise TypeError("document.passages must be a list")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise TypeError("max_candidates must be a non-negative integer")
    if max_candidates < 0:
        raise ValueError("max_candidates must be a non-negative integer")

    available_by_kind = {"abstract": 0, "fulltext": 0}
    empty_passages = 0
    total_matches = 0
    candidates = []
    for passage_index, passage in enumerate(passages):
        if not isinstance(passage, dict):
            raise TypeError("each passage must be a dictionary")
        text = passage.get("text", "")
        if not isinstance(text, str):
            raise TypeError("passage.text must be a string")
        if not text.strip():
            empty_passages += 1
            continue
        kind = passage.get("kind")
        if kind not in available_by_kind:
            raise ValueError("nonempty passage.kind must be abstract or fulltext")
        if passage.get("id") is None or passage.get("locator") is None:
            raise ValueError("nonempty passages require id and locator")
        available_by_kind[kind] += 1
        matches = _cues(text)
        categories = {item["category"] for item in matches}
        if len(categories & _RELEVANCE_CATEGORIES) < 2:
            continue
        total_matches += 1
        if len(candidates) >= max_candidates:
            continue
        candidate = {
            "passage_id": passage["id"],
            "passage_index": passage_index,
            "locator": passage["locator"],
            "kind": kind,
            "start": 0,
            "end": len(text),
            "quote": text,
            "context": text,
            "matched_cues": matches,
            "candidate_only": True,
        }
        if "section" in passage:
            candidate["section"] = passage["section"]
        candidates.append(candidate)

    available = sum(available_by_kind.values())
    remaining = total_matches - len(candidates)
    kinds = [kind for kind, count in available_by_kind.items() if count]
    input_scope = "mixed" if len(kinds) == 2 else kinds[0] if kinds else "none"
    limitations = [
        "Candidates are lexical matches, not extracted or verified clinical evidence.",
        "English cue coverage is limited; unmatched passages may still contain evidence.",
        "No matches never proves that the document contains no relevant evidence.",
        "Background, methods and negated results may all be candidates.",
        "Cross-passage context and table/figure/footnote relationships are not resolved.",
        "Fulltext kind describes supplied passages, not verified complete article coverage.",
    ]
    if input_scope == "abstract":
        limitations.append(
            "Only supplied abstracts were scanned; full text was not assessed."
        )
    if not available:
        limitations.append(
            "No nonempty source text is available; waiting for source material."
        )
    if remaining:
        limitations.append(
            "Candidate emission budget reached; remaining matches require a larger budget."
        )
    return {
        "version": VERSION,
        "method": "rules",
        "status": "waiting_source"
        if not available
        else "partial"
        if remaining
        else "completed",
        "coverage": {
            "input_scope": input_scope,
            "provided_passages": len(passages),
            "scanned_passages": available,
            "empty_passages": empty_passages,
            "available_by_kind": available_by_kind,
            "total_matches": total_matches,
            "emitted": len(candidates),
            "remaining": remaining,
            "max_candidates": max_candidates,
            "full_document_assessed": False,
        },
        "candidates": candidates,
        "limitations": limitations,
    }
