"""029's validated molecular clinical extension; the legacy prompt stays frozen."""

from apps.curation.knowledge_relation_prompt import DEFINITION as BASE_DEFINITION

VERSION = "target-clinical-focus-v1"
DEFINITION = {
    **BASE_DEFINITION,
    "version": VERSION,
    "system_prompt": BASE_DEFINITION["system_prompt"] + '''\nFor this molecular clinical evidence task, retain a molecular diagnostic relationship when ANCHOR explicitly establishes one, even without a drug or numeric endpoint. Use biomarker_association for a diagnostic molecular finding, explicitly name the diagnosis in population and state the diagnostic finding in results. This is not automatically a treatment claim. Distinguish actually detected genomic changes from immunostaining, prior treatment, VUS, and background. Preserve tested gene/variant spelling and numbering; do not reconcile conflicting spellings, guess fusion partners, expand broad alteration groups into individual mutations, or infer sensitivity from co-occurrence. A case molecular diagnosis must remain a case-level observation, not a sufficient universal diagnostic rule. If a statement lacks sufficient molecular specificity, preserve the broad wording; scripts will keep it out of the target-ready set. Do not add extra facts just to increase the count.''',
}
