"""Narrow source-grounded projection of prediction-model performance relations."""

import re
from copy import deepcopy

VERSION = "source-relation-types-v1"
MODEL_PERFORMANCE_RULE = "explicit_gene_model_performance-v1"
_MEASURE = re.compile(
    r"(?:net benefit(?: in decision curve analysis)?|model discrimination|"
    r"(?:optimism[-‐–]corrected )?AUC|Brier score|decision[-‐–]analysis value)", re.IGNORECASE
)
_GENE_MODEL = re.compile(r"gene[-‐–]clinical|molecular features|gene[-‐–]only", re.IGNORECASE)
_PERFORMANCE = re.compile(r"\b(?:ROC|AUC|DCA|Brier|calibration)\b|discriminat|net benefit|decision[- ]support", re.IGNORECASE)
_PHARMACOKINETICS = re.compile(r"pharmacokinetic|concentration[-‐– ]time|drug exposure", re.IGNORECASE)


def refine_relation(packet, original_fact):
    """Return a derived view; never revise the original model payload.

    Every result must explicitly concern model performance, and the ANCHOR
    must identify the gene/molecular model context. AUC alone never qualifies.
    Mixed clinical outcomes or conflicting pharmacokinetic context abstain.
    """
    fact = deepcopy(original_fact)
    unchanged = {"fact": fact, "changes": []}
    results = fact.get("results")
    if (fact.get("relation") != "treatment_outcome" or fact.get("conclusion")
            or not isinstance(results, list) or not results):
        return unchanged
    aliases = {p["alias"]: p for p in packet["passages"]}
    anchors = [p for p in packet["passages"] if p.get("role") == "anchor"]
    if not any(_GENE_MODEL.search(p["text"]) and re.search(r"\bmodels?\b", p["text"], re.IGNORECASE)
               and _PERFORMANCE.search(p["text"]) for p in anchors):
        return unchanged
    support = {}
    for result in results:
        if not isinstance(result, dict):
            return unchanged
        measure, group = result.get("measure"), result.get("group")
        passage = aliases.get(result.get("passage"))
        if (not isinstance(measure, str) or not _MEASURE.fullmatch(measure.strip())
                or not isinstance(group, str) or not re.search(r"\bmodels?\b", group, re.IGNORECASE)
                or not passage or result.get("passage") not in fact.get("evidence", [])
                or not re.search(r"\bmodels?\b", passage["text"], re.IGNORECASE)
                or not _PERFORMANCE.search(passage["text"])):
            return unchanged
        support[passage["id"]] = passage
    source = {p["id"]: p for p in [*anchors, *support.values()]}
    if any(_PHARMACOKINETICS.search(p["text"]) for p in source.values()):
        return unchanged
    fields = {"relation": {"before": fact["relation"], "after": "biomarker_association"}}
    fact["relation"] = "biomarker_association"
    # Existing biomarker records use treatment for the actual cohort exposure.
    # Keep that condition and schema intact; the relation identifies what is
    # being claimed (prediction-model performance, not therapeutic benefit).
    return {"fact": fact, "changes": [{
        "version": VERSION, "rule_id": MODEL_PERFORMANCE_RULE, "fields": fields,
        "reason": "All reported results describe source-confirmed gene/molecular prediction-model performance; treatment is cohort context.",
        "evidence": [{"passage_id": p["id"], "alias": p["alias"], "locator": p["locator"],
                      "version_id": p["version_id"], "source_sha256": p["source_sha256"],
                      "start": 0, "end": len(p["text"]), "quote": p["text"]} for p in source.values()],
        "results_conditions_and_source_unchanged": True, "model_calls": 0,
    }]}
