"""Prepare source-grounded, review-only mappings for the frozen 30-record batch.

No inference, target connection, target import, or source-fact changes. Exact
names are reused; explicit cancer crosswalks retain narrower qualifiers in the
companion. A target identity mapping is not semantic-quality approval.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "target-compatibility-mapping-v1"
BATCHES = {
    "minimal-selected": "minimal-knowledge-20261101",
    "minimal-e2e": "minimal-e2e-20261101",
}

# Explicit, inspectable business-level crosswalks. These are never fuzzy matches
# and do not erase the source's stage, subtype, or molecular restrictions.
CANCER_CROSSWALK = {
    "limited-stage small cell lung cancer": "小细胞肺癌",
    "metastatic castration-resistant prostate cancer (mcrpc)": "Prostate cancer",
    "metastatic castration-resistant prostate cancer": "Prostate cancer",
    "metastatic breast cancer": "Breast cancer",
    "her2+ breast cancer": "Breast cancer",
    "her2-positive metastatic breast cancer": "Breast cancer",
    "diffuse midline glioma": "弥漫性中线胶质瘤",
    "dmg": "弥漫性中线胶质瘤",
    "h3 k27m-mutant dmg": "弥漫性中线胶质瘤",
    "metastatic colorectal cancer": "Colorectal cancer",
    "mcrc": "Colorectal cancer",
    "muscle‐invasive bladder cancer": "Bladder cancer",
    "mibc": "Bladder cancer",
    "dtc": "Thyroid cancer",
    "differentiated thyroid cancer": "Thyroid cancer",
    "triple-negative breast cancer": "Breast cancer",
    "tnbc": "Breast cancer",
}
ACRONYM_SUPPORT = {
    "mcrc": "colorectal", "mibc": "bladder", "dtc": "thyroid",
    "tnbc": "breast", "dmg": "midline glioma",
}

# Each policy concerns target representation only, not correctness of the fact.
POLICIES = {
    "K01": ("none", "sequential", "一般治疗生存结局，无明确分子适用条件。诱导、同步放化疗和巩固有先后，不能压成同时联合。"),
    "K02": ("none", "sequential", "一般治疗总生存结局，无分子触发条件；诱导→同步放化疗→巩固顺序须保留。"),
    "K03": ("unsupported_marker", "sequential", "Ki-67≤80%是表达/增殖指标分层，不是DNA突变；不能生成MKI67 mutation触发器。"),
    "K04": ("none", "sequential", "已保存的是治疗生存知识，无分子适用条件；目标覆盖问题与本次目标结构适配分开。"),
    "K05": ("none", "alternatives", "三个ipilimumab+nivolumab剂量队列为备选方案，不是多个队列同时用药；没有分子触发条件。"),
    "K06": ("none", "combination", "双药安全性描述没有分子适用条件；不能由药物组合推定药敏或证据等级。"),
    "K07": ("unsupported_marker", "combination", "TREM2相关髓系表达签名与结局关联，不是TREM2突变；原文也未确定其为预后还是治疗预测标志物。"),
    "K08": ("unsupported_marker", "single", "CRP早期动态是血清蛋白指标，目标mutation/cnv/fusion无法表达；原文为无显著组间差异。"),
    "K09": ("unsupported_marker", "single", "CRP正常化分组是血清水平状态，不能变成CRP基因突变条件。"),
    "K10": ("unsupported_marker", "single", "HER2表达分层不能替换为ERBB2扩增或突变。T-DXd采用目标原名唯一精确条目，不改用同库的Fam-trastuzumab重复英文名条目。"),
    "K11": ("none", "single", "既往T-DM1暴露是治疗史，不能作为当前联合用药或分子trigger；T-DXd原名已有唯一目标ID，但本条仍没有分子适用条件。"),
    "K12": ("unsupported_marker", "combination", "HER2+为表达背景，S100是机制信息；不能强补DNA触发器。TTP488/TTP-488同药拼写和细胞实验语境保留。"),
    "K13": ("unsupported_marker", "combination", "S100/RAGE为信号机制，HER2-positive为表达背景；不得当作S100或RAGE突变。动物/转移模型不能自动转成人体药效。"),
    "K14": ("none", "none", "单例放疗结局中的H3F3A、KRAS、PHF6共突变是病例背景，不能据此创建放疗AND适应规则或拆成OR。当前未用temozolomide，Dexamethasone为支持治疗。"),
    "K15": ("unresolved", "single", "H3 K27M-mutant是家族范围，不能擅自缩成H3F3A K27M；现有15条规则没有等价H3家族规则。recurrent/progressive disease本身不是癌种实体。"),
    "E-42272324-0000": ("unsupported_marker", "single", "血清血管生成因子、Angiogenesis Index及Treg水平不是DNA事件；模型已保存的这些标记不能转成同名基因突变。"),
    "E-42272324-0001": ("unresolved", "single", "当前fact只保存研究目的/测量条件，缺明确结果与分子参与关系；不能从同篇摘要替它补成已提取的有效治疗结论。"),
    "E-42272324-0021": ("none", "none", "这是转移发生比例背景，没有治疗干预和分子触发条件；不强行套治疗证据中心载荷。"),
    "E-42050770-0000": ("none", "alternatives", "ddMVAC与D-GC是比较的备选新辅助方案，不能合成一组药物；当前抽取只有方案名、未形成全部具体药物参与实体。"),
    "E-42050770-0001": ("unresolved", "none", "该片段为antibody drug conjugate词项，fact无疾病、药物、关系结果；保留部分完成记录，不把同篇上下文伪装成已抽取关系。"),
    "E-42050770-0038": ("none", "alternatives", "ddMVAC或durvalumab+GC是两个备选方案，不能平铺成六药联合；没有分子适用条件。"),
    "E-42068240-0000": ("none", "alternatives", "lenvatinib+tigecycline和lenvatinib+eravacycline是两组组合，不能三药合并。PARP裂解为凋亡结果，不是PARP变异适应条件。"),
    "E-42068240-0001": ("none", "alternatives", "PARP为机制/结果蛋白，非分子适用trigger；两种抗生素分别与lenvatinib组合。原执行缺指定中心段引用，仍保留该缺口。"),
    "E-42068240-0022": ("none", "alternatives", "两组双药组合的体外效果，不支持三药联合，也没有分子触发条件；体外语境必须保留。"),
    "E-42858231-0001": ("unsupported_marker", "unspecified", "RecurIndex、基因表达、风险评分和临床分期是预测特征，不能转成OBSL1/BLM/CCR1突变；dose-dense NAC未给具体组成药物。"),
    "E-42858231-0002": ("unsupported_marker", "unspecified", "表达/风险评分模型与病理缓解关联，不等于具体DNA变异关系；淋巴结分期不是分子条件。NAC未落到具体药物。"),
    "E-42858231-0003": ("unsupported_marker", "unspecified", "RecurIndex是表达特征工具，目标现有DNA触发器不能表达；NAC具体药物仍未结构化。"),
    "E-42845214-0000": ("none", "alternatives", "WNT/RA是通路，ALDH阳性细胞比例是结果；不是DNA适用条件。多种药物的组合实验不可拼成六药同时治疗。"),
    "E-42845214-0001": ("none", "unspecified", "retinoic acid/retinoids为一般背景与转化局限，未形成可用分子条件；不能把RA类药统一指定为某个异构体药物。"),
    "E-42845214-0002": ("unresolved", "single", "APC泛突变/诱导野生型APC是细胞机制条件，未给具体可匹配位点；目标mutation没有任意突变通配。WNT、RA、神经内分泌细胞是机制或结果，不是附加AND基因。"),
}


def key(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def supports(record, needles=(), limit=2):
    """Return exact excerpts from stored anchors, never model-generated prose."""
    anchors = sorted(record["anchors"], key=lambda a: (a["purpose"] == "mention", -len(a["quote"])))
    found, seen = [], set()
    for anchor in anchors:
        text = anchor["quote"]
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if needles and not any(key(word) in key(sentence) for word in needles):
                continue
            pair = (anchor["passage_id"], sentence)
            if pair in seen or not sentence.strip():
                continue
            found.append({"passage_id": pair[0], "quote": sentence})
            seen.add(pair)
            if len(found) == limit:
                return found
    if not found and needles:
        return supports(record, (), limit=1)
    return found


def target_matches(label, rows):
    return [row for row in rows if key(label) in {key(row.get("name")), *[key(v) for v in row.get("aliases", [])]}]


def target_preview(row):
    return {k: row.get(k, "") for k in ("id", "name", "name_cn", "name_en")}


def map_entity(record, entity, dictionary):
    ref, label = entity["ref"], entity["label"]
    kind = "drugs" if entity["kind"] == "drug" else "tumors"
    matches = target_matches(label, dictionary[kind])
    notes = {"entity_ref": ref, "kind": entity["kind"], "label": label,
             "roles": record["fact"]["entity_roles"][ref], "target_id": None,
             "target_name": "", "target_name_cn": "", "status": "unmapped",
             "basis": "目标快照未找到唯一名称/别名精确对应；不使用组合药物条目替代单药。",
             "candidates": [target_preview(x) for x in matches],
             "source_support": supports(record, [label], 1)}
    if len(matches) == 1:
        notes.update(status="exact_target_name", basis="原词与目标名称/官方字段别名唯一精确匹配（仅忽略大小写与多余空白）。")
    elif len(matches) > 1:
        notes.update(status="ambiguous", basis="目标快照存在多个同名/别名UUID，保留歧义。")
    elif kind == "tumors" and key(label) in CANCER_CROSSWALK:
        target_name = CANCER_CROSSWALK[key(label)]
        matches = target_matches(target_name, dictionary[kind])
        if len(matches) != 1:
            raise ValueError(f"Crosswalk target no longer unique: {target_name}")
        term = ACRONYM_SUPPORT.get(key(label), label)
        support = supports(record, [term], 2)
        if not any(key(term) in key(item["quote"]) for item in support):
            raise ValueError(f"No original full-name support for {ref}")
        notes.update(status="explicit_crosswalk", basis=f"本次提议的主癌种预览映射：{label} → {target_name}；原文细分/分期/分子限定仍保留，不代表目标递归匹配亚型，也不把主癌种ID当作更宽泛人群已获支持。", source_support=support)
        notes["candidates"] = [target_preview(x) for x in matches]
    elif kind == "tumors" and key(label) == "recurrent or progressive disease":
        notes.update(status="not_identity", basis="该短语是病程状态，不能独立对应癌种UUID；不更改原抽取记录。")
    elif kind == "drugs" and key(label) == "t-dxd":
        matches = target_matches("Fam-trastuzumab deruxtecan-nxki", dictionary[kind])
        notes.update(status="ambiguous", basis="原文T-DXd的药名对应需显式别名确认；目标有两条Fam-trastuzumab deruxtecan-nxki，不能任取UUID。", candidates=[target_preview(x) for x in matches])
    if notes["status"] == "unmapped":
        # A substring can suggest a review candidate, never an identity mapping:
        # Etoposide/Olaparib is not etoposide alone.
        hints = [row for row in dictionary[kind] if len(key(label)) >= 4
                 and re.search(rf"(?<!\w){re.escape(key(label))}(?!\w)", key(row.get("name")))]
        notes["candidates"] = [{**target_preview(row), "candidate_kind": "name_contains_source_not_identity"} for row in hints]
        if hints:
            notes["basis"] += " 另列名称包含原词的候选供核对；可能为复方/备选列表，不能替代单药。"
        notes["basis"] += " 未命中不等于已证明目标字典不存在对应概念。"
    if notes["status"] in {"exact_target_name", "explicit_crosswalk"}:
        notes.update(target_id=matches[0]["id"], target_name=matches[0]["name"], target_name_cn=matches[0].get("name_cn", ""))
    return notes


def scopes(record):
    group = record["group_id"]
    trigger_kind, treatment_kind, reason = POLICIES[group]
    roles = record["fact"].get("entity_roles", {})
    entities = {e["ref"]: e for e in record["entities"]}
    molecular = [ref for ref, values in roles.items() if "molecular" in values]
    drug_refs = [e["ref"] for e in record["entities"] if e["kind"] == "drug" and "intervention" in roles.get(e["ref"], [])]
    treatment_reason = reason
    if group == "K15":
        primary = [ref for ref in drug_refs if entities[ref]["label"] == "dordaviprone"]
        aliases = [ref for ref in drug_refs if entities[ref]["label"] == "ONC201"]
        if (len(primary) != 1 or len(aliases) != 1
                or not any("dordaviprone (ONC201)" in a["quote"] for a in record["anchors"])):
            raise ValueError("K15 single-drug projection requires the original dordaviprone (ONC201) definition")
        drug_refs = primary
        treatment_reason += " 原文明确dordaviprone (ONC201)为同一药物；single投影以dordaviprone为主引用，ONC201保留在原知识及实体ID核对清单中，不计为第二种药物。"
    conditions = molecular[:] if trigger_kind in {"unsupported_marker", "unresolved"} else []
    if group in {"K12", "K13"}:
        conditions = []  # HER2 qualifier is in the disease phrase; S100/RAGE are mechanism.
    if group == "E-42845214-0002":
        conditions = [ref for ref in molecular if entities[ref]["label"] == "APC"]
    if group.startswith("E-42858231"):
        conditions = [ref for ref in conditions if entities[ref]["label"] != "clinical nodal stage"]
    condition_labels = [entities[ref]["label"] for ref in conditions]
    support_terms = ["HER2"] if group in {"K12", "K13"} else condition_labels
    trigger_support = supports(record, support_terms or [record["fact"].get("treatment") or "", "radiotherapy"])
    trigger = {"kind": trigger_kind, "condition_entity_refs": conditions,
               "non_trigger_entity_refs": [ref for ref in molecular if ref not in conditions],
               "logic": "none" if trigger_kind == "none" else "single" if len(conditions) == 1 else "unspecified",
               "source_support": trigger_support, "reason": reason}
    groups = []
    if group.startswith("E-42068240"):
        for antibiotic in ("tigecycline", "eravacycline"):
            refs = [ref for ref in drug_refs if key(entities[ref]["label"]) in {"lenvatinib", antibiotic}]
            groups.append({"entity_refs": refs, "label": f"lenvatinib with {antibiotic}"})
    elif group == "E-42050770-0038":
        for name, members in [("ddMVAC", {"methotrexate", "vinblastine", "doxorubicin", "cisplatin"}),
                              ("D‐GC", {"durvalumab", "gemcitabine", "cisplatin"})]:
            groups.append({"entity_refs": [ref for ref in drug_refs if key(entities[ref]["label"]) in members], "label": name})
    elif group == "E-42050770-0000":
        groups = [{"entity_refs": [], "label": "ddMVAC"}, {"entity_refs": [], "label": "D-GC"}]
    elif group == "K05":
        for label in ("I1+N360", "I3+N1", "I5+N1"):
            groups.append({"entity_refs": drug_refs, "label": label})
    elif treatment_kind in {"single", "combination"}:
        groups = [{"entity_refs": drug_refs, "label": record["fact"].get("treatment") or ""}]
    treatment = {"kind": treatment_kind, "entity_refs": drug_refs, "groups": groups,
                 "source_support": supports(record, [entities[ref]["label"] for ref in drug_refs] or ["radiotherapy"]),
                 "reason": treatment_reason}
    return trigger, treatment


def build(bundle, dictionary):
    mappings, notes = {}, []
    for record in bundle["records"]:
        group = record["group_id"]
        if group not in POLICIES:
            raise ValueError(f"No reviewed target-compatibility policy for {group}")
        roles = record["fact"].get("entity_roles", {})
        participating = [e for e in record["entities"] if
                         (e["kind"] == "drug" and "intervention" in roles.get(e["ref"], [])) or
                         (e["kind"] == "disease" and "disease" in roles.get(e["ref"], []))]
        entities = [map_entity(record, entity, dictionary) for entity in participating]
        trigger, treatment = scopes(record)
        ids = {e["entity_ref"]: e["target_id"] for e in entities if e["target_id"]}
        crosswalks = [f"{e['entity_ref']}: {e['basis']}" for e in entities if e["target_id"]]
        mappings[group] = {"entity_ids": ids, "basis": "\n".join(crosswalks) or "无可确认目标实体ID；不生成UUID。",
                           "trigger_scope": trigger, "treatment_scope": treatment,
                           "trigger_bindings": [], "review_only": True}
        points = [trigger["reason"]]
        if any(e["kind"] == "disease" and e["target_id"] for e in entities):
            points.append("癌种ID只确认目标身份；目标报告按癌种中文名匹配，不自动递归子癌种。主癌种映射仍须保留原文人群限定。")
        unresolved = [e for e in entities if not e["target_id"]]
        if unresolved:
            points.append("待核对的实体：" + "、".join(f"{e['label']}（{e['status']}）" for e in unresolved))
        if record["fact"].get("relation") != "treatment_outcome":
            points.append("原关系类型不能唯一决定目标证据类别，保留类别待定；不由关联自动推定治疗敏感性。")
        if record.get("assessment", {}).get("execution", {}).get("status") == "partial":
            points.append("原批次执行状态为部分完成，本次只做映射预览，没有补写原抽取答案。")
        notes.append({"group_id": group, "source_run": bundle["source_run"],
                      "pmid": str(record.get("article", {}).get("pmid", "")),
                      "entity_mappings": entities, "trigger_scope": trigger, "treatment_scope": treatment,
                      "review_points": points, "semantic_quality_reviewed": False,
                      "target_written": False, "submit_ready": False})
    return mappings, notes


def write_frozen(path, data):
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text() != text:
            raise ValueError(f"Refusing to overwrite {path}; choose a new output version")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as out:
            out.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "var/experiments/target-compatibility-20261101")
    parser.add_argument("--output-version", default="v3", help="Append a new immutable mapping version")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.output_version):
        raise ValueError("Invalid output version")
    dictionary_path = args.root / "target-dictionary.json"
    dictionary = json.loads(dictionary_path.read_text())
    records, sources = [], {}
    for name, batch in BATCHES.items():
        path = ROOT / "var/experiments" / batch / "delivery/bundle.json"
        bundle = json.loads(path.read_text())
        mappings, notes = build(bundle, dictionary)
        write_frozen(args.root / f"{name}-mappings-{args.output_version}.json", mappings)
        records.extend(notes)
        sources[name] = {"path": str(path.relative_to(ROOT)), "sha256": digest(path), "source_run": bundle["source_run"]}
    if len(records) != 30:
        raise ValueError("Expected exactly the frozen 30-record batch")
    entity_rows = [e for r in records for e in r["entity_mappings"]]
    statuses = Counter(e["status"] for e in entity_rows)
    counts = {"records": len(records), "participating_drug_disease_refs": len(entity_rows),
              "exact_identity_refs": statuses["exact_target_name"],
              "explicit_cancer_crosswalk_refs": statuses["explicit_crosswalk"],
              "mapped_identity_refs": sum(bool(e["target_id"]) for e in entity_rows),
              "unmapped_identity_refs": sum(not e["target_id"] for e in entity_rows),
              "identity_statuses": dict(statuses), "trigger_scope_kinds": dict(Counter(r["trigger_scope"]["kind"] for r in records)),
              "treatment_scope_kinds": dict(Counter(r["treatment_scope"]["kind"] for r in records)),
              "unique_drug_ids": len({e["target_id"] for e in entity_rows if e["kind"] == "drug" and e["target_id"]}),
              "unique_tumor_ids": len({e["target_id"] for e in entity_rows if e["kind"] == "disease" and e["target_id"]})}
    result = {"schema_version": VERSION, "source": {"bundles": sources,
              "dictionary_sha256": digest(dictionary_path), "snapshot_id": dictionary["source"]["snapshot_id"],
              "policy": "既有原文与目标字典的适配核对；非新的知识语义质量审核、非临床结论确认。"},
              "counts": counts, "records": records, "target_written": False, "submit_ready": False}
    write_frozen(args.root / f"review-notes-{args.output_version}.json", result)
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
