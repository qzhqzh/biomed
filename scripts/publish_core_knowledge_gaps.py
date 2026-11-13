"""Register research 013 from frozen artifacts without production writes."""

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from apps.curation.research_history import prepare_record, read_record, register_record
from scripts.evaluate_core_knowledge import indexed, require
from scripts.evaluate_knowledge_candidates import read, sha

RID = "core-gaps-20261029"
ROOT = PROJECT / "var/experiments" / RID
BASE = PROJECT / "var/experiments/core-knowledge-20261029"
DOC = "docs/research/013-optional-entities-and-source-evidence.md"


def verify_quotes(value, passages):
    if isinstance(value, dict):
        if "passage_id" in value and "quote" in value:
            passage = passages[value["passage_id"]]
            start = value.get("quote_start", value.get("start"))
            end = value.get("quote_end", value.get("end"))
            require(
                type(start) is int
                and type(end) is int
                and passage["text"][start:end] == value["quote"],
                "Audit quote does not match original English",
            )
            if "passage_text_sha256" in value:
                require(
                    hashlib.sha256(passage["text"].encode()).hexdigest()
                    == value["passage_text_sha256"],
                    "Audit paragraph hash differs",
                )
        for child in value.values():
            verify_quotes(child, passages)
    elif isinstance(value, list):
        for child in value:
            verify_quotes(child, passages)


def card(packet, row, audit):
    fact = row["raw_model_fact"] or {}
    statement = fact.get("statement")
    selected = set(fact.get("entity_ids", []))
    deferred = {ref["entity_id"] for ref in row["unresolved_entity_refs"]}
    entities = []
    for entity in packet["entities"]:
        if entity["id"] not in selected:
            continue
        cid = (entity.get("link") or {}).get("candidate_id")
        entities.append(
            {
                "id": entity["id"],
                "type": entity["type"],
                "text": entity["text"],
                "normalization_status": "附加实体未决"
                if entity["id"] in deferred
                else "未确认标准候选"
                if cid
                else "unresolved",
                "standard_ids": [cid] if cid else [],
                "source_anchored": bool(entity.get("locations")),
            }
        )
    excerpts = []
    if packet["id"] == "K07":
        excerpts = [
            role["evidence"]
            for role in audit["core_roles"]
            if role["role"] == "core_association_and_direction"
        ]
        statement = excerpts[0]["quote"]
        note = "当前展示原文，旧模型概括在展开字段。P=0.054只支持趋势表述。TREM2+ TAMs是未定位的附加ID，不是核心药物、癌种或签名；移入未决项后可导出，未改事实或语义审核。"
        scope = "前列腺癌联合治疗队列；签名关联趋势，不是7条独立耐药基因结论"
    elif packet["id"] == "K12":
        excerpts = [audit["actual_error"]["evidence"]]
        statement = excerpts[0]["quote"]
        note = "当前展示原文，旧模型概括在展开字段。原文明确是cells；错误在旧模型conditions/scope主动把4 nM写为mouse models，不是原文不清。原始候选继续阻断；本轮未重跑9B或生成替代知识。"
        scope = "当前摘录为细胞/体外证据；旧模型scope保留在展开字段，不等同人体疗效"
    else:
        note = "当前主文仍为012旧模型生成陈述，供对照；原始英文全文段在证据中，013未重新生成概括。"
        scope = fact.get("scope") or "原输出未提供"
    note += f" 核心流程审核{'通过' if row['resolved_review']['core_usable'] else '未通过'}，技术检查{'通过' if row['technical_ready_for_export'] else '未通过'}。"
    if row["review"].get("duplicate_of"):
        note += f" 与{row['review']['duplicate_of']}有语义重叠，不宣称独立新知识。"
    quotes = []
    for passage in packet["passages"]:
        cited = passage["alias"] in fact.get("evidence", [])
        quotes.append(
            {
                "passage_id": passage["id"],
                "locator": f"{passage['alias']} / {passage['role'].upper()} / "
                + ("原模型引用" if cited else "原模型输入上下文")
                + " · "
                + passage["locator"],
                "quote": passage["text"],
                "fields": ["原模型候选，未逐字段绑定"] if cited else [],
                "start": 0,
                "end": len(passage["text"]),
                "version_id": passage["version_id"],
                "source_sha256": passage["source_sha256"],
                "parser_version": passage["parser_version"],
                "coordinate_space": "parsed_passage_unicode_codepoints",
            }
        )
    return {
        "id": packet["id"],
        "pmid": packet["article"]["pmid"],
        "title": packet["article"]["title"],
        "statement": statement,
        "status": "supported" if row["ready_for_export"] else "needs_revision",
        "review_note": note,
        "study_scope": scope,
        "source_role": "业务癌种与证据上下文分开；保留完整英文原段",
        "entities": entities,
        "quotes": quotes,
        "article_provenance": packet["article"],
        "mapping": {
            "status": "not_run",
            "blockers": [
                "本轮为研究记录，未导入生产知识库，未修改tumor_api。",
                "标准ID仍为候选；附加未决项保留，不自动确认或补位置。",
            ],
            "preview": {
                "旧模型概括及完整原始事实": fact,
                "核验过的原文摘录": excerpts,
                "原始Astra审核": row["review"],
                "012名称对齐后的流程审核（未重评）": row["resolved_review"],
                "原词类型分流": row["projection"],
                "附加实体未决": row["unresolved_entity_refs"],
                "延期变更": row["optional_entity_changes"],
                "仍保留的阻断": row["resolved_review"]["blocking"],
                "Astra定向核验": audit or None,
            },
        },
    }


def ratio(label, count, note):
    return {"label": label, "numerator": count, "denominator": 15, "note": note}


def build():
    evaluation = read(ROOT / "evaluation.json")
    for source in evaluation["sources"]:
        require(
            sha(Path(source["path"])) == source["sha256"], "Evaluated source changed"
        )
    audit = read(ROOT / "astra-gap-audit.json")
    require(audit["model"] == "gpt-6-astra", "Actual Astra audit required")
    for name, digest in audit["source_sha256"].items():
        require(sha(BASE / name) == digest, "Audited input changed")
    packets = read(ROOT / "packets.json")["records"]
    require(
        read(ROOT / "packets.json") == read(BASE / "packets.json"),
        "Original packets changed",
    )
    verify_quotes(audit, {p["id"]: p for packet in packets for p in packet["passages"]})
    rows = indexed(evaluation["records"], "evaluation")
    audits = indexed(audit["records"], "audit")
    cards = [
        card(packet, rows[packet["id"]], audits.get(packet["id"])) for packet in packets
    ]
    counts = evaluation["scores"]
    require(
        len(cards) == 15
        and sum(c["status"] == "supported" for c in cards)
        == counts["ready_for_export"],
        "Card counts differ",
    )
    require(not rows["K12"]["ready_for_export"], "K12 must remain blocked")
    prior = PROJECT / "apps/curation/research_data" / f"{RID}.json"
    date = (
        read_record(RID)["date"]
        if prior.exists()
        else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    )
    artifact_names = [
        "evaluation.json",
        "packets.json",
        "knowledge-items.json",
        "knowledge-items.jsonl",
        "astra-gap-audit.json",
        "quote-projection-discussion.json",
        "executed-code.json",
        "decisions.json",
    ]
    provenance = [
        {"label": name, "artifact": f"{RID}/{name}", "sha256": sha(ROOT / name)}
        for name in artifact_names
    ]
    for name in [
        DOC,
        "scripts/publish_core_knowledge_gaps.py",
        "scripts/evaluate_core_knowledge_gaps.py",
        "apps/curation/core_knowledge_optional_entities.py",
    ]:
        provenance.append(
            {"label": name, "artifact": name, "sha256": sha(PROJECT / name)}
        )
    record = {
        "schema_version": "research-history-v1",
        "id": RID,
        "title": "013 · 附加实体解耦与英文原文核验",
        "date": date,
        "stage": "entities",
        "status": "evaluated",
        "task_kind": "knowledge_candidates",
        "sample_count": 15,
        "sample_unit": "片段组",
        "summary": "复用15组旧输出，附加实体延期使可导出研究条目13→14；核心审核仍14/15，K12生成条件错绑继续阻断。零新9B调用，尚未测概括整体利弊。",
        "task": {
            "question": "能否延期非核心的未定位实体，并分清原文事实与生成字段错误？",
            "input": "012同5篇15组冻结原文、实体、9B结果及名称对齐审核。",
            "outputs": [
                "K07附加实体未决及可导出状态",
                "K12错误字段与精确英文证据核验",
                "保留原始审核与双层出处的14条研究条目",
            ],
            "not_measured": [
                "取消生成概括的整体收益或损失",
                "新的9B抽取效果",
                "全库泛化与独立测试准确率",
                "临床适用性或生产导入",
            ],
        },
        "dataset": {
            "label": "同5篇15组开发回归集",
            "document_count": 5,
            "role": "开发回归集",
            "notes": [
                "复用既有输出，没有新增9B推理。",
                "K04/K01部分重叠，14条不是14条独立事实。",
            ],
        },
        "reference": {
            "label": "012冻结审核＋实际Astra对K07/K12定向核验",
            "status": "model_reference_not_human_gold",
            "note": "全组语义评分不变；Astra核验原句和错误定位，不是人工金标准，也不是新模型成绩。",
        },
        "methods": [
            {
                "id": "optional-entity-deferral",
                "label": "附加实体与核心事实解耦",
                "version": evaluation["optional_entity_version"],
                "metrics": [
                    ratio(
                        "处理前可导出",
                        counts["before_ready_for_export"],
                        "012名称对齐后结果。",
                    ),
                    ratio(
                        "处理后可导出",
                        counts["ready_for_export"],
                        "新增K07，K12仍阻断；不是语义准确率上升。",
                    ),
                    ratio(
                        "核心流程审核（不变）",
                        counts["core_usable"],
                        "沿用012resolved_review；原始模型核心及原名评分仍13/15。",
                    ),
                    ratio(
                        "结构与定位通过",
                        counts["structure_valid"],
                        "仅延期K07.e20，不回填位置或ID。",
                    ),
                    {
                        "label": "新增9B调用",
                        "value": 0,
                        "unit": "次",
                        "note": "Astra仅研究核验，不计为9B生产推理。",
                    },
                    {
                        "label": "脚本处理",
                        "value": round(evaluation["timing"]["script_seconds"], 4),
                        "unit": "秒",
                        "note": "15组验证、延期、组装投影；不含文件读写、导出及Astra。",
                    },
                ],
                "conclusion": "13→14的增量来自技术解耦；未改变核心语义审核。",
            }
        ],
        "findings": [
            "K07核心药物、癌种、签名与趋势关系有出处；TREM2+ TAMs只是附加ID，延期后保留原选择及诊断。",
            "P=0.054按原文保留趋势，不升级显著性，不拆成7条独立耐药基因关系。",
            "K12原文清楚写cells，错误在生成conditions/scope把4 nM错绑mouse models；旧候选仍不可导出。",
            "K07/K12卡片主文显示精确英文原文；旧模型概括在展开字段，其余卡片仍显示旧陈述供对照。",
            "乳腺癌是癌种，细胞/动物/人体是证据上下文；不自动删除临床前研究。",
            "013没有概括有无的整体对照；单例quote方案仅前瞻讨论、未运行，不能决定取消概括。",
        ],
        "next_steps": [
            "同15组、同模型比较原概括契约与较窄输出契约，测错误、遗漏、可用性和耗时；本记录尚无该轮结果。",
            "保留原始英文及完整段落，使候选实体和证据可以检索、追溯；不把摘录展示误当关系结构化已完成。",
        ],
        "identity": {
            "task_version": evaluation["version"],
            "completeness": "frozen",
            "sample_versions": [sha(ROOT / "packets.json")],
            "method_versions": [
                sha(ROOT / name)
                for name in ["evaluation.json", "executed-code.json", "decisions.json"]
            ],
            "reference_versions": [
                sha(ROOT / "astra-gap-audit.json"),
                sha(BASE / "name-resolution.json"),
            ],
        },
        "provenance": provenance,
        "knowledge_candidates": cards,
    }
    return prepare_record(record)


if __name__ == "__main__":
    print(register_record(build()))
