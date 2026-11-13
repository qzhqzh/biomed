"""Check a model-reference's article and verbatim excerpt provenance.

Exact source quotations do not establish that the model's interpretation is
correct. This validator never changes a reference or a knowledge candidate.
"""

VERSION = "biomed.astra-source-reference-integrity.v1"


def validate(source_only, reference, *, expected_pmids):
    if (reference.get("reviewer") != "gpt-6-astra"
            or reference.get("reference_type") != "source_only_model_reference_not_expert_gold"):
        raise ValueError("Reference identity or review scope differs")
    sources = {str(row["pmid"]): row for row in source_only.get("articles", [])}
    if len(sources) != len(source_only.get("articles", [])):
        raise ValueError("Duplicate article in source packet")
    records = reference.get("records")
    if not isinstance(records, list) or {str(row.get("pmid")) for row in records} != set(expected_pmids):
        raise ValueError("Review articles differ from expected frozen sample")
    if len(records) != len(set(expected_pmids)):
        raise ValueError("Duplicate review article")
    fact_count = uncertainty_count = evidence_count = 0
    for record in records:
        pmid = str(record["pmid"])
        if pmid not in sources:
            raise ValueError("Reference cites an unknown article")
        passages = {str(p["id"]): p for p in sources[pmid]["passages"]}
        if len(passages) != len(sources[pmid]["passages"]):
            raise ValueError("Duplicate source passage")
        facts, uncertainties = record.get("facts"), record.get("uncertainties")
        if not isinstance(facts, list) or not isinstance(uncertainties, list):
            raise TypeError("Facts and uncertainties must be arrays")
        fact_count += len(facts)
        uncertainty_count += len(uncertainties)
        for item in [*facts, *uncertainties]:
            if not isinstance(item, dict):
                raise TypeError("Review entries must be objects")
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list) or (item in facts and not evidence):
                raise ValueError("A referenced fact must include source evidence")

        def check_evidence(value, passages=passages, pmid=pmid):
            nonlocal evidence_count
            if isinstance(value, list):
                for child in value:
                    check_evidence(child)
            elif isinstance(value, dict):
                for field, child in value.items():
                    if field != "evidence":
                        check_evidence(child)
                        continue
                    if not isinstance(child, list):
                        raise TypeError("Source evidence must be an array")
                    for quote in child:
                        passage = passages.get(str(quote.get("passage_id"))) if isinstance(quote, dict) else None
                        text = quote.get("quote") if isinstance(quote, dict) else None
                        if (not passage or quote.get("locator") != passage.get("locator")
                                or not isinstance(text, str) or not text or text not in passage.get("text", "")):
                            raise ValueError(f"Reference quote does not match source: {pmid}")
                        evidence_count += 1

        check_evidence(record)
    return {"version": VERSION, "articles": len(records), "facts": fact_count, "uncertainties": uncertainty_count,
            "source_quotes_verified": evidence_count, "semantic_correctness_proved": False,
            "reference_type": "source_only_model_reference_not_expert_gold"}
