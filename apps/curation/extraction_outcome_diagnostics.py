"""Explain an existing extraction attempt without changing stored results.

A model's empty answer is not ground truth. In particular, it must not be
reported as a model failure or trigger the same recovery as truncated output.
"""

VERSION = "biomed.extraction-outcome-diagnostics.v1"


def diagnose(entity_result, relation_result):
    entity_status = (entity_result or {}).get("status")
    relation_status = (relation_result or {}).get("status")
    if entity_result is None:
        outcome, action = "entity_result_missing", "inspect_or_resume_existing_attempt"
    elif entity_status == "output_truncated":
        outcome, action = "entity_output_truncated", "bounded_retry_with_full_source_and_new_version"
    elif entity_status != "classified":
        outcome, action = "entity_output_failed", "inspect_failure_before_retry"
    elif not isinstance(entity_result.get("payload"), dict) or not isinstance(entity_result["payload"].get("entities"), list):
        outcome, action = "entity_output_invalid", "inspect_failure_before_retry"
    elif relation_result is None:
        outcome, action = "relation_result_missing", "inspect_or_resume_existing_attempt"
    elif relation_status == "output_truncated":
        outcome, action = "relation_output_truncated", "bounded_retry_with_full_source_and_new_version"
    elif relation_status != "classified":
        outcome, action = "relation_output_failed", "inspect_failure_before_retry"
    elif not isinstance(relation_result.get("payload"), dict) or not isinstance(relation_result["payload"].get("facts"), list):
        outcome, action = "relation_output_invalid", "inspect_failure_before_retry"
    elif not relation_result["payload"]["facts"]:
        outcome, action = "model_returned_no_fact", "retain_source_and_model_reason"
    else:
        outcome, action = "facts_returned", "check_source_and_target_mapping"
    return {"version": VERSION, "outcome": outcome, "next_action": action,
            "entity_model_status": entity_status, "relation_model_status": relation_status,
            "model_no_fact_proves_absence": False, "automatic_retry_started": False}
