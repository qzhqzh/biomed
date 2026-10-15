"""Small, explicit demo rules. No medical inference or external model requests."""

import re
from uuid import uuid4

from .models import CaseFields, Evidence

CANCERS = ("非小细胞肺癌", "肺腺癌", "乳腺癌", "胃癌", "结直肠癌")
DRUGS = ("奥希替尼", "吉非替尼", "化疗")
NEGATED = re.compile(r"未|无|否认|排除|疑似|可能|待|不是|阴性|不详|不确定")


def extract_fields(text: str) -> dict:
    fields = CaseFields()
    evidence: dict[str, str] = {}
    clauses = re.split(r"[，。；;\n,]", text)
    # Intentionally conservative: ambiguous clauses stay empty for manual confirmation.
    for clause in clauses:
        if NEGATED.search(clause):
            continue
        for cancer in CANCERS:
            if cancer in clause:
                fields.cancer = cancer
                evidence["cancer"] = clause.strip()
        stage = re.search(r"(?<![A-Z])(IV|III|II|I|[一二三四1-4])\s*期", clause.upper())
        if stage:
            fields.stage = {
                "一": "I",
                "二": "II",
                "三": "III",
                "四": "IV",
                "1": "I",
                "2": "II",
                "3": "III",
                "4": "IV",
            }.get(stage[1], stage[1])
            evidence["stage"] = clause.strip()
        if re.search(r"EGFR\s*19\s*(?:外显子)?\s*(?:缺失|DEL)", clause, re.I):
            fields.biomarker = "EGFR 19外显子缺失"
            evidence["biomarker"] = clause.strip()
        elif re.search(r"HER2\s*阳性", clause, re.I):
            fields.biomarker = "HER2 阳性"
            evidence["biomarker"] = clause.strip()
        if re.search(r"既往|用过|服用|接受过", clause):
            used = [name for name in DRUGS if name in clause]
            if used:
                fields.prior_therapy = "、".join(used)
                evidence["prior_therapy"] = clause.strip()
        for city in ("上海", "北京", "杭州", "广州", "深圳"):
            if city in clause:
                fields.city = city
                evidence["city"] = clause.strip()
    warnings = ["当前使用有限词表提取，请逐项核对；未识别内容保持空白。"]
    if NEGATED.search(text):
        warnings.append("检测到否定或不确定表述，相关分句未自动填入。")
    return {"fields": fields.model_dump(), "evidence": evidence, "warnings": warnings}


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def search_evidence(fields: CaseFields, catalog: list[Evidence]) -> dict:
    run_id = str(uuid4())
    if not fields.cancer:
        return {
            "run_id": run_id,
            "status": "insufficient",
            "results": [],
            "excluded": [],
            "message": "请先补充癌种。空白字段不会被当作已满足条件。",
        }
    results, excluded = [], []
    for item in catalog:
        if normalized(fields.cancer) != normalized(item.cancer):
            continue
        matched = [f"癌种：{item.cancer}"]
        unknown, conflicts = [], []
        for key, label in (("stage", "分期"), ("biomarker", "分子特征"), ("city", "城市")):
            required = getattr(item, key)
            actual = getattr(fields, key)
            if not required:
                continue
            if not actual or (key == "city" and actual == "不限"):
                unknown.append(f"{label}待核对：条目要求 {required}")
            elif normalized(actual) != normalized(required):
                conflicts.append(f"{label}不一致：输入 {actual}，条目要求 {required}")
            else:
                matched.append(f"{label}：{required}")
        if item.kind == "drug" and item.drug_name:
            used = re.split(r"[、,，;；\n]", fields.prior_therapy)
            if normalized(item.drug_name) in {normalized(name) for name in used}:
                conflicts.append("该示例药物已出现在既往治疗字段中")
        if item.kind == "trial":
            unknown.append("完整入排条件、研究中心实际招募状态尚未核对")
        if item.kind == "drug":
            unknown.append("本条为虚构教学记录，不能据此判断适应症或用药适用性")
        result = {
            **item.model_dump(),
            "matched": matched,
            "unknown": unknown,
            "conflicts": conflicts,
            "match_state": "excluded"
            if conflicts
            else ("needs_review" if unknown else "demo_match"),
        }
        (excluded if conflicts else results).append(result)
    return {
        "run_id": run_id,
        "status": "matched" if results else "no_match",
        "results": results,
        "excluded": excluded,
        "message": "以下仅为教学资料匹配，逐条查看依据。"
        if results
        else ("未匹配到示例条目；这不代表没有相关治疗或试验。"),
    }
