"""Minimal diagnostic extension to the existing 9B treatment screening task."""

from copy import deepcopy

VERSION = "solid-molecular-clinical-passage-screening-v1"


def definition_from_legacy(legacy):
    if legacy.get("version") != "solid-treatment-passage-screening-v1":
        raise ValueError("Clinical screening requires the known frozen treatment definition")
    definition = deepcopy(legacy)
    definition["version"] = VERSION
    definition["prompt"] = definition["prompt"].replace(
        "contains an extractable therapeutic evidence statement about a solid tumor",
        "contains an extractable therapeutic or molecular diagnostic evidence statement about a solid tumor",
        1,
    )
    definition["prompt"] += '''\n\nAlso return relevant=true for an explicit molecular diagnostic finding: CENTER reports a detected genomic mutation, fusion/rearrangement or copy-number event in an actual patient/case or tumor pathology, and explicitly connects that finding to a named tumor diagnosis or subtype. A qualitative case-level diagnostic association qualifies without a drug, treatment outcome or numeric endpoint. This actual diagnostic-finding exception takes precedence over the baseline/assay-only exclusions above; an assay protocol alone still does not qualify. Neighbor paragraphs may clarify the patient and disease, but the diagnostic finding must be in CENTER. Do not require or invent a fusion partner.\nReturn relevant=false for isolated gene/variant mentions, immunostaining or protein expression without a genomic diagnostic finding, VUS without an explicit diagnostic association, generic molecular/diagnostic background, mere co-occurrence of a gene and tumor, speculative potential, and planned tests. A diagnostic term or genomic event alone does not establish a diagnostic relationship. Do not infer a diagnosis from general medical knowledge. Preserve the same one-key relevant boolean/null output. Prognostic-only findings are not added by this extension.'''
    definition["scope_notes"] = [*definition.get("scope_notes", []),
                                  "Adds explicit patient/pathology genomic diagnostic findings; no prognostic-only expansion."]
    return definition
