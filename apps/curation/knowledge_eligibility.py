"""Cheap, source-preserving routing checks; never a semantic correctness score.

An eligible record has a minimally represented relationship. A deferred record
remains available for inspection/re-extraction; this does not assert that the
original article contains no useful knowledge.
"""

VERSION = "knowledge-structural-eligibility-v2"
CORE_RELATIONS = {"treatment_outcome", "biomarker_association", "mechanism", "recommendation"}
# Exact observed method-only values, not a general statistical-word classifier.
# Additional text (including a P/AUC estimate or qualitative finding) prevents
# this narrow match, so genuine results are not rejected for lacking numbers.
METHOD_ONLY_VALUES = {"p-values derived from wilcoxon rank-sum tests", "exploratory roc analysis"}
SOURCE_ISSUES = {
    "target_anchor_not_cited", "invalid_evidence", "model_output_not_complete",
    "unlocated_participant", "invalid_relation", "missing_entities", "invalid_entity_roles",
}


def anchor_eligibility(passage):
    """A glossary definition can supply context but is not an evidence center."""
    tags = passage.get("ancestor_tags", [])
    reasons = ["glossary_context_only"] if isinstance(tags, list) and "def-list" in tags else []
    return {"version": VERSION, "status": "deferred" if reasons else "eligible", "reasons": reasons,
            "structure_assessed": isinstance(tags, list) and bool(tags)}


def assess_record(record, *, passages=()):
    """Derive routing from a fact and optional original paragraph structure.

    Minimal input is {fact, assessment?}; stored entity_roles are sufficient.
    No numbers, detailed subtype, standard ID, or human/model review is required.
    Legacy records without a role map are not assumed to have zero participants.
    """
    fact = record.get("fact")
    fact = fact if isinstance(fact, dict) else {}
    reasons = []
    if not fact:
        reasons.append("no_extracted_fact")
    else:
        relation = fact.get("relation")
        conclusion = fact.get("conclusion")
        has_conclusion = isinstance(conclusion, str) and bool(conclusion.strip())
        # "Other" plus participants and method/context slots is not itself a
        # relationship. A qualitative conclusion counts; a number is not needed.
        if relation not in CORE_RELATIONS and not has_conclusion and not fact.get("results"):
            reasons.append("no_core_relation")
        results = fact.get("results")
        if (not has_conclusion and relation not in {"recommendation", "mechanism"}
                and isinstance(results, list) and results and all(
                    isinstance(result, dict) and isinstance(result.get("value"), str)
                    and " ".join(result["value"].casefold().strip().rstrip(".").split()) in METHOD_ONLY_VALUES
                    for result in results)):
            reasons.append("methods_only_results")
    roles = fact.get("entity_roles")
    participants_assessed = isinstance(roles, dict)
    if participants_assessed and not any(
        isinstance(values, list) and any(role in {"disease", "intervention", "comparator", "molecular"}
                                        for role in values)
        for values in roles.values()
    ):
        reasons.append("no_located_participants")
    assessment = record.get("assessment") or {}
    execution = assessment.get("execution") or {}
    issues = execution.get("issues") or []
    reasons.extend(issue for issue in issues if isinstance(issue, str) and issue in SOURCE_ISSUES)
    structure_assessed = False
    centers = [p for p in passages if p.get("role") == "anchor"]
    # New bundles carry structure on anchors; old raw facts remain unchanged.
    centers += [a.get("attributes", {}) for a in record.get("anchors", [])
                if a.get("attributes", {}).get("input_role") == "anchor"]
    for center in centers:
        routed = anchor_eligibility(center)
        structure_assessed |= routed["structure_assessed"]
        reasons.extend(routed["reasons"])
    return {"version": VERSION, "status": "deferred" if reasons else "eligible",
            "reasons": sorted(set(reasons)), "participants_assessed": participants_assessed,
            "structure_assessed": structure_assessed, "semantic_correctness": "unassessed"}
