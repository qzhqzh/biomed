"""Compare explicitly sourced study context without deciding patient eligibility."""


def compare_reported_context(requirements, observed):
    """Keep information searchable; report consistent, different, or unknown context.

    Values must already be normalized by the caller. This function neither
    extracts requirements nor infers eligibility or treatment recommendations.
    """
    if not isinstance(requirements, list) or not isinstance(observed, dict):
        raise TypeError("Expected a requirement list and a context object")
    checks, fields = [], set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise TypeError("Requirement must be an object")
        field, expected, source = requirement.get("field"), requirement.get("expected"), requirement.get("source")
        if (not isinstance(field, str) or not field or field in fields
                or not isinstance(expected, str) or not expected.strip()
                or not isinstance(source, dict) or not (source.get("pmid") or source.get("version_id"))
                or not source.get("passage")):
            raise ValueError("Each context field needs one value and a specific source")
        fields.add(field)
        known_values = requirement.get("known_values", [expected])
        if (not isinstance(known_values, list) or not known_values
                or any(not isinstance(item, str) or not item.strip() for item in known_values)):
            raise ValueError("Known context values must be non-empty strings")
        value = observed.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError("Context values must be normalized strings or absent")
        normalized = " ".join((value or "").split()).casefold()
        expected_normalized = " ".join(expected.split()).casefold()
        known = {" ".join(item.split()).casefold() for item in known_values}
        if expected_normalized not in known:
            raise ValueError("Expected context must be present among known values")
        if normalized in {"", "unknown", "未提供", "未确定"} or normalized not in known:
            status = "unknown"
        elif normalized == expected_normalized:
            status = "consistent"
        else:
            status = "different"
        checks.append({"field": field, "expected": expected, "observed": value, "status": status, "source": dict(source)})
    statuses = {check["status"] for check in checks}
    status = ("different_from_reported_context" if "different" in statuses else
              "unknown_context" if "unknown" in statuses else
              "consistent_with_reported_context" if checks else "no_context_to_compare")
    return {"status": status, "checks": checks, "informational_retrieval": True,
            "determines_patient_eligibility": False, "treatment_recommendation": False}
