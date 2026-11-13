"""Freeze five existing full-text documents for offline passage screening.

Reads an existing experiment snapshot and its immutable local JATS blobs only.
Never accesses a database, network, model, or production output directory.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from defusedxml import ElementTree

SCHEMA_VERSION = "evidence-passages-v1"
SELECTION = {
    "42869886": {
        "tumor": "局限期小细胞肺癌",
        "study_type": "随机II期临床试验",
        "reason": "比较camrelizumab诱导/巩固方案与同步放化疗，包含生存、缓解、安全性与研究条件。",
    },
    "42873849": {
        "tumor": "转移性去势抵抗性前列腺癌",
        "study_type": "免疫联合治疗剂量扩展临床队列",
        "reason": "比较ipilimumab联合nivolumab不同剂量的疾病控制、安全性及耐药关联，覆盖有限获益结果。",
    },
    "42870461": {
        "tumor": "胆道癌",
        "study_type": "多中心回顾性观察队列",
        "reason": "研究化免治疗患者CRP动态与缓解、生存的关联，可检验治疗反应标志物与因果疗效的区分。",
    },
    "42467226": {
        "tumor": "转移性乳腺癌",
        "study_type": "临床样本关联与细胞/动物转化研究",
        "reason": "T-DXd耐药与RAGE抑制联合策略同时包含人体观察和临床前干预，检验证据层级及适用条件。",
    },
    "42872145": {
        "tumor": "成人丘脑弥漫中线胶质瘤",
        "study_type": "单病例报告及叙述性讨论",
        "reason": "包含实际放疗、明确未用temozolomide、影像反应及疑似进展，区分实际治疗与背景药物讨论。",
    },
}
NOT_SELECTED = {
    "42272324": "适合后续研究；当前已有胆道癌治疗生物标志物队列，优先补充病例和转化研究类型。",
    "42050770": "系统综述及网状meta分析，当前先评估原始研究/病例；后续另测二手转述与原始试验关联。",
    "42860552": "重点为放疗后转移预测模型，当前优先直接治疗效果、安全性和耐药研究。",
    "42871965": "重点为胃癌发生发展机制，当前优先有明确药物或放疗干预的证据。",
    "42068240": "适合后续研究；本轮已包含乳腺癌临床前联合策略，优先保留更多临床研究类型。",
}
LIMITATIONS = [
    "本轮完整覆盖既有解析器产出的所有全文p段落，包括JATS摘要、正文及正文中嵌套图注/表注；不按规则候选筛样。",
    "PubMed元数据摘要单列留存，不重复加入评分；全文自身的JATS摘要保持原样参与评分。",
    "现有解析器展平p内文本，部分表格文字可能拼入上级段落，但未恢复单元格关系、图像内容或补充文件。",
    "嵌套p或图注可能同时出现在上级段落与独立段落，文本内容有重叠；本轮不改解析器或删除这些既有段落。",
    "只评估已解析段落集合，不能据此宣称全篇全部表图证据被覆盖；同一事实重复提及不等于独立证据。",
    "目的性选择的五篇开发试验，非随机留出集，不能推断全部实体瘤文献的总体性能。",
]


def digest(value):
    """Match apps.curation.decision.digest without loading Django."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def node_text(node):
    return " ".join("".join(node.itertext()).split())


def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def load_verified_version(library, version):
    if version["integrity_status"] != "valid":
        raise ValueError("Only valid source versions may be frozen")
    path = library / "blobs" / version["sha256"][:2] / version["sha256"]
    raw = path.read_bytes()
    if sha256_bytes(raw) != version["sha256"]:
        raise ValueError(f"Source hash mismatch: {version['id']}")
    return path, raw


def verify_document(document, library):
    version = next(
        row
        for row in document["versions"]
        if row["id"] == document["fulltext_version_id"]
    )
    if version["media_type"] != "application/jats+xml":
        raise ValueError("This frozen protocol expects existing JATS full text")
    path, raw = load_verified_version(library, version)
    root = ElementTree.fromstring(raw)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    article = root if root.tag == "article" else root.find(".//article")
    ids = {
        node.attrib.get("pub-id-type"): node_text(node)
        for node in article.findall("front/article-meta/article-id")
    }
    if ids.get("pmid") != document["pmid"]:
        raise ValueError("Full-text article PMID mismatch")
    parent_map = {child: parent for parent in article.iter() for child in parent}
    parsed = {}
    for section in ("front/article-meta/abstract", "body"):
        area = article.find(section)
        if area is not None:
            for index, node in enumerate(area.iter("p"), 1):
                if node_text(node):
                    parsed[f"{section}/p:{index}"] = node
    passages = document["passages"]
    if digest(passages) != document["passages_sha256"]:
        raise ValueError("Snapshot passage digest mismatch")
    if set(parsed) != {row["locator"] for row in passages}:
        raise ValueError(
            "Stored passages do not exactly cover parsed source paragraphs"
        )
    contexts = []
    for index, row in enumerate(passages):
        if (
            row["kind"] != "fulltext"
            or row["version_id"] != version["id"]
            or row["ordinal"] != index
            or node_text(parsed[row["locator"]]) != row["text"]
        ):
            raise ValueError("Passage source, ordinal, or exact locator text mismatch")
        node = parsed[row["locator"]]
        ancestors = []
        while node in parent_map:
            node = parent_map[node]
            ancestors.append(node.tag)
        contexts.append(
            {
                "id": row["id"],
                "locator": row["locator"],
                "ancestor_tags": ancestors,
                "nested_paragraph_ancestor": "p" in ancestors,
                "figure_context": "fig" in ancestors,
                "table_context": "table-wrap" in ancestors,
            }
        )
    metadata_version = next(
        row
        for row in document["versions"]
        if row["id"] == document["metadata_version_id"]
    )
    metadata_path, _ = load_verified_version(library, metadata_version)
    coverage = {
        "parsed_fulltext_passages": len(passages),
        "verified_exact_locator_texts": len(parsed),
        "abstract_passages": sum(
            row["locator"].startswith("front/") for row in passages
        ),
        "body_passages": sum(row["locator"].startswith("body/") for row in passages),
        "jats_table_count": len(list(article.iter("table"))),
        "jats_figure_count": len(list(article.iter("fig"))),
        "jats_supplementary_material_count": len(
            list(article.iter("supplementary-material"))
        ),
        "paragraphs_nested_in_paragraphs": sum(
            row["nested_paragraph_ancestor"] for row in contexts
        ),
        "paragraphs_in_figure_context": sum(row["figure_context"] for row in contexts),
        "paragraphs_in_table_context": sum(row["table_context"] for row in contexts),
        "metadata_abstract_scored": False,
        "table_structure_extracted": False,
        "image_content_extracted": False,
        "supplementary_files_opened": False,
        "limitations": LIMITATIONS,
    }
    provenance = {
        "pmid": document["pmid"],
        "title": document["title"],
        "material_id": document["material_id"],
        "source_key": document["source_key"],
        "fulltext_version": version,
        "fulltext_blob_path": str(path),
        "fulltext_source_sha256_verified": True,
        "article_identifiers": ids,
        "metadata_version": metadata_version,
        "metadata_blob_path": str(metadata_path),
        "metadata_source_sha256_verified": True,
        "metadata": document["metadata"],
        "source_passages_sha256": document["passages_sha256"],
        "coverage": coverage,
        "paragraph_structure": contexts,
    }
    return version, provenance


def prepare(snapshot_path, output):
    raw_snapshot = snapshot_path.read_bytes()
    envelope = json.loads(raw_snapshot)
    snapshot = envelope["snapshot"]
    if digest(snapshot) != envelope["sha256"]:
        raise ValueError("Source snapshot envelope digest mismatch")
    library = Path(snapshot["readable_library"])
    documents = {row["pmid"]: row for row in snapshot["documents"]}
    rows, summaries, provenance_files = [], [], {}
    for pmid, selection in SELECTION.items():
        document = documents[pmid]
        version, provenance = verify_document(document, library)
        summary = {
            "pmid": pmid,
            "title": document["title"],
            "version_id": version["id"],
            "source_sha256": version["sha256"],
            "passage_count": len(document["passages"]),
            "provenance_file": f"documents/{pmid}.json",
        }
        summaries.append(summary)
        provenance_files[pmid] = {**provenance, "selection": selection}
        for index, passage in enumerate(document["passages"]):
            row = {
                "id": passage["id"],
                "pmid": pmid,
                "title": document["title"],
                "version_id": passage["version_id"],
                "source_sha256": version["sha256"],
                "parser_version": passage["parser_version"],
                "locator": passage["locator"],
                "ordinal": passage["ordinal"],
                "text": passage["text"],
                "previous_text": document["passages"][index - 1]["text"]
                if index
                else "",
                "next_text": document["passages"][index + 1]["text"]
                if index + 1 < len(document["passages"])
                else "",
            }
            rows.append({**row, "input_sha256": digest(row)})
    if len(rows) != 282 or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Unexpected paragraph count or duplicate IDs")
    inputs = {"schema_version": SCHEMA_VERSION, "documents": summaries, "records": rows}
    selection = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": str(snapshot_path.resolve()),
        "source_snapshot_file_sha256": sha256_bytes(raw_snapshot),
        "source_snapshot_payload_sha256": envelope["sha256"],
        "source_snapshot_payload_sha256_verified": True,
        "selection_basis": "Existing solid10 titles, metadata abstracts and full-text passages; therapeutic relevance and diversity of tumor/study type. No rule-candidate count, model output or new reference label used.",
        "selected_pmids": list(SELECTION),
        "candidate_documents": [
            {
                "pmid": document["pmid"],
                "title": document["title"],
                "parsed_fulltext_passages": len(document["passages"]),
                "selected": document["pmid"] in SELECTION,
                "rationale": SELECTION.get(document["pmid"], {}).get("reason")
                or NOT_SELECTED[document["pmid"]],
            }
            for document in snapshot["documents"]
        ],
        "selected_documents": [
            {
                **summary,
                **SELECTION[summary["pmid"]],
                "source_sha256_verified": True,
                "pmid_in_jats_verified": True,
                "all_locator_texts_verified": True,
            }
            for summary in summaries
        ],
        "input_records_sha256": digest(rows),
        "inputs_payload_sha256": digest(inputs),
        "paragraph_count": len(rows),
        "independent_holdout": False,
        "training": False,
        "model_prediction_used_for_selection": False,
        "rule_candidate_used_for_selection": False,
        "database_access": False,
        "network_access": False,
        "new_downloads": False,
        "limitations": LIMITATIONS,
    }
    output.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite an existing frozen input or its provenance.
    targets = [output / "inputs.json", output / "selection.json"]
    targets += [output / "documents" / f"{pmid}.json" for pmid in SELECTION]
    targets += [output / f"reference-inputs-{pmid}.json" for pmid in SELECTION]
    if any(path.exists() for path in targets):
        raise FileExistsError(
            "Frozen output already exists; choose a new experiment directory"
        )
    save_new(output / "inputs.json", inputs)
    save_new(output / "selection.json", selection)
    for summary in summaries:
        pmid = summary["pmid"]
        save_new(output / "documents" / f"{pmid}.json", provenance_files[pmid])
        save_new(
            output / f"reference-inputs-{pmid}.json",
            {
                "schema_version": SCHEMA_VERSION,
                "documents": [summary],
                "records": [row for row in rows if row["pmid"] == pmid],
            },
        )
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "documents": len(summaries),
                "passages": len(rows),
                "input_records_sha256": digest(rows),
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("var/experiments/evidence-pilot-20261022-solid10/snapshot.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/experiments/evidence-passages-20261023"),
    )
    args = parser.parse_args()
    prepare(args.snapshot, args.output)


if __name__ == "__main__":
    main()
