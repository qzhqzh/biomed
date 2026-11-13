"""Keep article type and source population separate from model assertions.

Signals route uncertain candidates; they do not prove the source is correct or
discard useful review and preclinical knowledge from biomed.
"""

import copy
import re

VERSION = "biomed.source-study-context.v1"
REVIEW_TYPES = {"review", "systematic review", "meta-analysis"}
PRECLINICAL = re.compile(r"\b(?:pdx|xenografts?|in\s+vitro|cell\s+lines?|mouse\s+models?|"
                         r"mice|murine|animal\s+models?)\b", re.IGNORECASE)
RECEIPT = re.compile(r"\b(?:proportion|percentage|number|fraction|rate)\b.{0,100}\b(?:receiv\w*|"
                     r"given|treated\s+with)\b", re.IGNORECASE)


def assess(sample, linked=None):
    document, record = sample["source_document"], sample["record"]
    metadata = document.get("metadata") or {}
    types = metadata.get("publication_types")
    types = types if isinstance(types, list) else []
    review_type = any(str(value).casefold().strip() in REVIEW_TYPES for value in types)
    linked = linked or {}
    assertion = linked.get("source_scope")
    review_primary_conflict = review_type and assertion == "primary"
    fact = record.get("fact") or {}
    population = str(fact.get("population") or "")
    cited = [a for a in record.get("anchors", []) if a.get("purpose") == "evidence"]
    preclinical = bool(PRECLINICAL.search(population) and any(PRECLINICAL.search(a.get("quote", "")) for a in cited))
    results = fact.get("results") if isinstance(fact.get("results"), list) else []
    receipt = any(isinstance(item, dict) and RECEIPT.search(str(item.get("measure") or "")) for item in results)
    return {"version": VERSION, "article_publication_types": types,
            "article_is_review_by_metadata": review_type, "model_source_scope": assertion,
            "review_primary_conflict": review_primary_conflict,
            "preclinical_population_explicit_in_fact_and_evidence": preclinical,
            "result_describes_treatment_receipt": receipt,
            "model_statement_independently_verified": False,
            "automatic_clinical_matching": False,
            "scope_note": "Review metadata flags a conflict, not proof that every review sentence is cited work. "
                          "Study population and endpoint still require semantic review.",
            "source_record_group_id": record.get("group_id"),
            "fact_context": copy.deepcopy({"population": population, "results": results})}


def guard(sample, preview):
    """Add source context to an opt-in preview, preserving the old baseline."""
    result = dict(preview)
    result["study_context"] = assess(sample, result.get("linked_fact"))
    if (result["study_context"]["review_primary_conflict"]
            and result.get("status") != "baseline_preserved"):
        result["previous_guard_result"] = {"status": result.get("status"), "reason": result.get("reason")}
        result["status"] = "deferred"
        result["reason"] = "review_publication_conflicts_with_primary_assertion"
    return result
