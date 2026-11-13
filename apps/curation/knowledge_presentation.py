"""Information-only evidence labels and narrow, source-bound context options."""

import re

from apps.curation.evidence_scope_context import compare_reported_context

VERSION = "biomed.knowledge-presentation.v1"
LINE = re.compile(r"\b(first|second|third)[- ]line(?:\s+(?:therapy|treatment))?\b", re.IGNORECASE)
LINE_FACT = re.compile(r"^\s*(first|second|third)[- ]line(?:\s+(?:therapy|treatment))?\s*$", re.IGNORECASE)
HISTOLOGY = re.compile(r"\b(low|high)[- ]grade\s+serous\s+ovarian\s+(?:carcinoma|cancer)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:no|not|without|excluding|exclude|lacking)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE)
LEVELS = {value: f"已记录 {value} 级" for value in ("A", "B", "C", "D")}
EFFECTS = {"S": "已记录敏感", "R": "已记录耐药", "C": "已记录冲突"}
LINE_LABELS = {"first-line therapy": "一线治疗", "second-line therapy": "二线治疗", "third-line therapy": "三线治疗"}
HISTOLOGY_LABELS = {"low-grade serous ovarian carcinoma": "低级别浆液性卵巢癌",
                    "high-grade serous ovarian carcinoma": "高级别浆液性卵巢癌"}


def _valid_sources(sources, version_id):
    valid = []
    if not isinstance(sources, list):
        return valid
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("passage"), dict):
            continue
        passage = source["passage"]
        text, quote = passage.get("text"), source.get("quote")
        start, end = source.get("start"), source.get("end")
        if (str(passage.get("version_id")) != str(version_id) or not isinstance(text, str)
                or not isinstance(quote, str) or type(start) is not int or type(end) is not int
                or start < 0 or end <= start or end > len(text) or text[start:end] != quote
                or not source.get("passage_id") or str(passage.get("id")) != str(source["passage_id"])
                or not passage.get("locator")):
            continue
        valid.append(source)
    return valid


def _source_for(sources, pattern, expected, *, field, result_alias=None):
    for source in sources:
        # A treatment-line result must use the passage attached to that result;
        # an earlier general background mention can describe a different group.
        if result_alias and result_alias not in source.get("aliases", []):
            continue
        fields = source.get("fields")
        if fields is not None and (
            not isinstance(fields, list) or field not in {
                re.split(r"[.\[]", value, maxsplit=1)[0] for value in fields if isinstance(value, str)
            }
        ):
            continue
        for match in pattern.finditer(source["quote"]):
            if match.group(1).casefold() != expected or NEGATION.search(source["quote"][max(0, match.start() - 40):match.start()]):
                continue
            start, end = max(0, match.start() - 160), min(len(source["quote"]), match.end() + 600)
            return {"passage": str(source["passage_id"]), "locator": source["passage"]["locator"],
                    "start": source["start"] + start, "end": source["start"] + end,
                    "quote": source["quote"][start:end]}
    return None


def _requirement(field, expected, labels, source, *, pmid, version_id):
    source = {**source, "pmid": str(pmid or ""), "version_id": str(version_id)}
    requirement = {"field": field, "expected": expected, "known_values": list(labels), "source": source}
    choices = []
    for value, label in labels.items():
        evaluated = compare_reported_context([requirement], {field: value})
        choices.append({"value": value, "label": label, "status": evaluated["checks"][0]["status"]})
    return {"field": field, "label": "治疗线" if field == "therapy_line" else "病理亚型",
            "reported_value": expected, "reported_label": labels[expected], "source": source, "choices": choices}


def evidence_presentation(fact, sources, *, version_id, pmid=None):
    """Preserve unknowns; do not infer sensitivity or patient eligibility.

    Only two documented language patterns are currently structured, and only
    when the same version contains an exact source quote. All other conditions
    stay in the original fact and are not classified as absent or applicable.
    """
    fact = fact if isinstance(fact, dict) else {}
    raw_level = fact.get("evidence_level")
    level = raw_level.strip().upper() if isinstance(raw_level, str) else ""
    raw_effect = fact.get("potency")
    effect = raw_effect.strip().upper() if isinstance(raw_effect, str) else ""
    level_result = {"status": "recorded" if level in LEVELS else "unknown", "label": LEVELS.get(level, "未分级")}
    effect_result = {"status": "recorded" if effect in EFFECTS else "unknown", "label": EFFECTS.get(effect, "未判定")}
    sources = _valid_sources(sources, version_id)
    conditions = fact.get("conditions") if isinstance(fact.get("conditions"), list) else []
    lines = {match.group(1).casefold() for value in conditions if isinstance(value, str)
             if (match := LINE_FACT.fullmatch(value))}
    requirements, unresolved = [], []
    if len(lines) == 1:
        line = next(iter(lines))
        results = fact.get("results") if isinstance(fact.get("results"), list) else []
        result_aliases = {item.get("passage") for item in results if isinstance(item, dict)
                          and isinstance(item.get("passage"), str) and item.get("passage")}
        result_alias = next(iter(result_aliases)) if len(result_aliases) == 1 else None
        matching_passages = {source["passage_id"] for source in sources
                             if result_alias and result_alias in source.get("aliases", [])}
        # If a result exists, missing or ambiguous result provenance cannot be
        # replaced by an unrelated background passage mentioning the same line.
        source = (_source_for(sources, LINE, line, field="conditions", result_alias=result_alias)
                  if not results or (len(results) == 1 and result_alias and len(matching_passages) == 1)
                  else None)
        if source:
            requirements.append(_requirement("therapy_line", f"{line}-line therapy", LINE_LABELS, source,
                                             pmid=pmid, version_id=version_id))
        else:
            unresolved.append("治疗线已提取，但未找到同版本、同字段的明确原文位置。")
    elif len(lines) > 1:
        unresolved.append("提取结果包含多个治疗线，不能自动确定当前结果属于哪一组。")
    population = fact.get("population") if isinstance(fact.get("population"), str) else ""
    grades = {match.group(1).casefold() for match in HISTOLOGY.finditer(population)}
    if len(grades) == 1:
        grade = next(iter(grades))
        source = _source_for(sources, HISTOLOGY, grade, field="population")
        if source:
            requirements.append(_requirement("histology", f"{grade}-grade serous ovarian carcinoma", HISTOLOGY_LABELS,
                                             source, pmid=pmid, version_id=version_id))
        else:
            unresolved.append("亚型已提取，但未找到同版本、同字段的明确原文位置。")
    elif len(grades) > 1:
        unresolved.append("提取结果包含多个亚型，不能自动确定当前结果的范围。")
    return {"version": VERSION, "evidence_level": level_result, "drug_effect": effect_result,
            "reported_context": {"requirements": requirements, "unresolved": unresolved,
                                 "other_conditions_preserved": bool(conditions),
                                 "automatic_patient_eligibility": False},
            "informational_retrieval": True, "treatment_recommendation": False}
