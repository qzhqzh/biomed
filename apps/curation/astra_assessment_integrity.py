"""Validate source-bound metadata for an independent model assessment."""

VERSION = "biomed.astra-assessment-integrity.v1"
JUDGMENTS = {"supported", "unsupported", "uncertain", "not_released"}


def validate(source, comparison, assessment, *, expected_pmids):
    if (assessment.get("reviewer") != "gpt-6-astra"
            or assessment.get("reference_type") != "source_only_reference_based_offline_semantic_assessment_not_expert_gold"):
        raise ValueError("Assessment identity or scope differs")
    articles = {str(row["pmid"]): row for row in source.get("articles", [])}
    rows = {str(row["pmid"]): row for row in comparison.get("records", [])}
    records = assessment.get("records")
    if not isinstance(records, list) or {str(row.get("pmid")) for row in records} != set(expected_pmids):
        raise ValueError("Assessment articles differ from frozen selection")
    if len(records) != len(set(expected_pmids)):
        raise ValueError("Duplicate assessment article")
    counts = {name: 0 for name in JUDGMENTS}
    quotes = 0
    for record in records:
        pmid = str(record["pmid"])
        previous, article = rows.get(pmid), articles.get(pmid)
        if (not previous or not article or record.get("group_id") != previous.get("group_id")
                or record.get("new_status") != previous["new_preview"]["status"]):
            raise ValueError(f"Assessment candidate differs from frozen comparison: {pmid}")
        judgment = record.get("core_judgment")
        if judgment not in JUDGMENTS:
            raise ValueError("Unknown reference judgment")
        counts[judgment] += 1
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Assessment lacks source evidence")
        passages = {str(p["id"]): p for p in article["passages"]}
        for item in evidence:
            passage = passages.get(str(item.get("passage_id"))) if isinstance(item, dict) else None
            text = item.get("quote") if isinstance(item, dict) else None
            if (not passage or item.get("locator") != passage.get("locator")
                    or not isinstance(text, str) or not text or text not in passage.get("text", "")):
                raise ValueError(f"Assessment quote does not match source: {pmid}")
            quotes += 1
    return {"version": VERSION, "articles": len(records), "judgments": counts,
            "source_quotes_verified": quotes, "semantic_correctness_proved": False,
            "reference_type": "independent_model_assessment_not_expert_gold"}
