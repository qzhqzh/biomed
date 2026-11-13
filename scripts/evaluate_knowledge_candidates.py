"""Offline evaluation and deliberately non-importable target previews. No service writes."""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.curation.decision import digest
from apps.curation.knowledge_candidates import ENTITY_TYPES, validate_candidate

VERSION = "knowledge-candidate-evaluation-v1"
VERDICTS = {
    "supported",
    "needs_revision",
    "rejected",
    "unsupported",
    "abstained",
    "invalid_output",
}
STATUSES = {"classified", "invalid_json", "output_truncated", "input_too_long"}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def parse(text):
    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def read(path):
    return parse(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path, value):
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text() != data:
            raise ValueError(f"Immutable artifact differs: {path}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".freeze-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text() != data:
                raise ValueError(f"Immutable artifact differs: {path}") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def key(text):
    return " ".join(text.casefold().split())


def text(value):
    return isinstance(value, str) and bool(value.strip())


def record_map(rows, label):
    if not isinstance(rows, list):
        raise TypeError(f"{label}: records must be an array")
    mapped = {}
    for row in rows:
        if not isinstance(row, dict) or not text(row.get("id")) or row["id"] in mapped:
            raise ValueError(f"{label}: invalid or duplicate record ID")
        mapped[row["id"]] = row
    return mapped


def indexes(standards):
    result = defaultdict(list)
    for concept in standards["concepts"]:
        release = standards["releases"][concept["release_id"]]
        namespace = release["namespace"].upper()
        kind = {"MONDO": "disease", "RXNORM": "drug"}.get(namespace)
        if not kind:
            continue
        record = {
            "namespace": namespace,
            "identifier": str(concept["identifier"]),
            "label": concept["label"],
            "version": release["version"],
            "source_sha256": release["sha256"],
        }
        for alias in [concept["label"], *concept["synonyms"]]:
            if text(alias):
                result[(kind, key(alias))].append(record)
    for gene in standards["genes"]:
        if not gene.get("gene_id"):
            continue
        record = {
            "namespace": "NCBIGene",
            "identifier": str(gene["gene_id"]),
            "label": gene["symbol"],
            "version": standards["gene_version"],
        }
        for alias in [gene["symbol"], *gene.get("synonyms", [])]:
            if text(alias):
                result[("gene", key(alias))].append(record)
    return result


def normalize(entity, index, source_anchored=True):
    """Never link an unanchored mention; keep it visible in the failure denominator."""
    valid_shape = isinstance(entity, dict) and all(
        text(entity.get(k)) for k in ("id", "type", "text", "passage_id")
    )
    if not valid_shape:
        return None  # Original malformed value remains in candidate/model output.
    matches = []
    status = "invalid_entity" if entity["type"] not in ENTITY_TYPES else "unanchored"
    if source_anchored and entity["type"] in ENTITY_TYPES:
        matches = list(
            {
                digest(row): row
                for row in index.get((entity["type"], key(entity["text"])), [])
            }.values()
        )
        status = (
            "exact_alias_unique"
            if len(matches) == 1
            else "ambiguous"
            if matches
            else "unresolved"
        )
    return {
        **entity,
        "source_anchored": source_anchored,
        "normalization_status": status,
        "standard_candidates": matches,
        "standard_ids": [
            (
                r["identifier"]
                if ":" in r["identifier"]
                else f"{r['namespace']}:{r['identifier']}"
            )
            for r in matches
        ]
        if len(matches) == 1
        else [],
        "target_internal_uuid": None,
        "note": "仅对通过原文定位的实体作现有字典大小写/空白归一精确别名匹配；未消歧，不冒充目标内部UUID。",
    }


def normalized_entities(candidate, validation, index):
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("entities"), list
    ):
        return []
    verified = {
        (s["entity_id"], s["entity_type"], s["quote"], s["passage_id"])
        for s in validation["entity_spans"]
        if all(
            isinstance(s.get(k), str)
            for k in ("entity_id", "entity_type", "quote", "passage_id")
        )
    }
    # Duplicate entity IDs cannot safely be linked to their relation references.
    ids = Counter(
        e.get("id")
        for e in candidate["entities"]
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    )
    output = []
    for entity in candidate["entities"]:
        if not isinstance(entity, dict) or any(
            not isinstance(entity.get(k), str)
            for k in ("id", "type", "text", "passage_id")
        ):
            continue
        anchored = (
            set(entity) == {"id", "type", "text", "passage_id"}
            and bool(re.fullmatch(r"e[1-9][0-9]*", entity["id"]))
            and entity["type"] in ENTITY_TYPES
            and (entity["id"], entity["type"], entity["text"], entity["passage_id"])
            in verified
            and ids[entity["id"]] == 1
        )
        normalized = normalize(entity, index, anchored)
        if normalized:
            output.append(normalized)
    return output


def mapping(packet, candidate, normalized, validation, review):
    blockers = [
        "未读取目标数据库，Drug/Tumor/TargetRule内部UUID尚未对照。",
        "目标每条conclusion要求非空trigger_inputs；本轮未生成已核实的mutation/cnv/fusion触发键或既有规则UUID。",
        "目标写入结构没有文章外键、原文跨度或逐字段引用；这些信息保存在biomed伴随记录。",
    ]
    result = {
        "status": "blocked",
        "api_ready": False,
        "preview": {},
        "blockers": blockers,
        "policy": "一条候选对应一条ClinicalEvidence和一个conclusion；不凭结果方向生成S/R，不推定A-D证据等级。",
        "semantic_sidecar_fields": [
            "article",
            "passages",
            "entity_spans",
            "evidence_spans",
            "field_supports",
            "conditions",
            "limitations",
            "study_scope",
            "source_role",
            "outcome",
            "comparison",
            "molecular_logic",
            "model_provenance",
            "reference_review",
        ],
    }
    if validation["status"] != "structurally_valid_candidate":
        blockers.append(
            "无完整通过结构/引用检查的知识候选；原始失败输出保留，不生成伪造映射字段。"
        )
        return result
    if review.get("verdict") != "supported":
        blockers.append("Astra语义复核未完全通过，须修订候选。")
    if candidate["study_scope"] != "human":
        blockers.append("非明确人体研究结论，必须保留研究层级，不能外推临床适用。")
    if candidate["source_role"] != "current_study":
        blockers.append(
            "二手/推荐/监管转述需保留来源角色，不能冒充本研究或已核实的监管事实。"
        )
    if any(
        e["type"] in ("protein", "biomarker") and e["id"] in candidate["molecular_ids"]
        for e in normalized
    ):
        blockers.append("蛋白表达/生物标志物不能改写成DNA mutation触发条件。")
    result["preview"] = {
        "evidence_description": candidate["statement"],
        "evidence_description_en": candidate["statement"],
        "evidence_category": "treatment",
        "evidence_source": f"PMID:{packet['article_pmid']}; DOI:{packet['article'].get('doi') or ''}; biomed:{packet['id']}",
        "evidence_level": "-",
        "evidence_type": candidate["source_role"],
        "comment": "离线未完成映射预览；原文、条件及审核记录保存在biomed伴随数据。",
        "conclusion_inputs": [
            {
                "potency": "-",
                "drug_ids": [],
                "disease_scope_inputs": [],
                "trigger_inputs": [],
            }
        ],
    }
    return result


def checked_result(result, packet, manifest):
    if (
        not isinstance(result, dict)
        or result.get("id") != packet["id"]
        or result.get("input_sha256") != packet["input_sha256"]
        or result.get("config_sha256") != manifest["config_sha256"]
    ):
        raise ValueError("Result identity changed")
    status, raw = result.get("status"), result.get("raw_output")
    if (
        not isinstance(status, str)
        or status not in STATUSES
        or not isinstance(raw, str)
    ):
        raise ValueError("Result status/raw_output invalid")
    if status == "classified":
        parsed = parse(raw)
        if not isinstance(parsed, dict) or digest(parsed) != digest(
            result.get("payload")
        ):
            raise ValueError("Result payload differs from strict raw JSON")
    elif result.get("payload") is not None:
        raise ValueError("Failed result must not carry repaired payload")
    if status == "invalid_json":
        try:
            parsed = parse(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            raise ValueError("invalid_json status contradicts raw JSON object")
    if status == "input_too_long" and (raw or result.get("output_tokens") != 0):
        raise ValueError("input_too_long result contains generated text")
    if any(
        type(result.get(k)) is not int or result[k] < 0
        for k in ("input_tokens", "output_tokens")
    ):
        raise ValueError("Result token counts invalid")
    seconds = result.get("seconds")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Result timing invalid")


def checked_reviews(paths, reference_paths, expected, definition_sha):
    references, reference_owners = {}, {}
    for path in reference_paths:
        part = read(path)
        if (
            part.get("definition_sha256") != definition_sha
            or part.get("annotator_model") != "gpt-6-astra"
            or part.get("status") != "model_reference_not_human_gold"
            or part.get("blinded_to_candidate_outputs") is not True
        ):
            raise ValueError("Reference definition/provenance mismatch")
        for ident, row in record_map(part.get("records"), "reference").items():
            if ident in references:
                raise ValueError("Duplicate reference ID across parts")
            references[ident], reference_owners[ident] = row, sha(path)
    reviews = {}
    for path in paths:
        part = read(path)
        if (
            part.get("state") != "completed"
            or part.get("reviewer_model") != "gpt-6-astra"
            or part.get("reference_frozen_before_review") is not True
            or part.get("definition_sha256") != definition_sha
        ):
            raise ValueError(
                "Review must be completed with frozen definition/reference identity"
            )
        items = record_map(part.get("records"), "review")
        if "expected" in part and (
            type(part["expected"]) is not int or part["expected"] != len(items)
        ):
            raise ValueError("Review part coverage mismatch")
        for ident, row in items.items():
            if (
                ident in reviews
                or ident not in references
                or part.get("reference_sha256") != reference_owners[ident]
            ):
                raise ValueError("Review reference hash mismatch or duplicate ID")
            if (
                not isinstance(row.get("verdict"), str)
                or row["verdict"] not in VERDICTS
                or not text(row.get("note"))
                or not isinstance(row.get("errors"), list)
            ):
                raise ValueError("Review verdict/note/errors invalid")
            if any(
                not isinstance(e, dict) or not text(e.get("category"))
                for e in row["errors"]
            ):
                raise ValueError("Review error category invalid")
            reviews[ident] = row
    if set(references) != expected or set(reviews) != expected:
        raise ValueError("Reference/review must cover every frozen input exactly once")
    return references, reviews


def build(base, output, standards_path, reviews):
    base, output, standards_path = Path(base), Path(output), Path(standards_path)
    reviews = [Path(path) for path in reviews]
    inputs, definition = read(base / "inputs.json"), read(base / "definition.json")
    packets = record_map(inputs["records"], "inputs")
    if not packets or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", ident)
        for ident in packets
    ):
        raise ValueError("Empty or unsafe packet IDs")
    documents = inputs.get("documents")
    if not isinstance(documents, list) or any(
        not isinstance(d, dict) or not text(d.get("pmid")) for d in documents
    ):
        raise ValueError("Document metadata invalid")
    document_map = {d["pmid"]: d for d in documents}
    if len(document_map) != len(documents) or any(
        p.get("article") != document_map.get(p.get("article_pmid"))
        for p in packets.values()
    ):
        raise ValueError("Packet article metadata differs from frozen document list")
    manifest, summary = read(output / "manifest.json"), read(output / "summary.json")
    config = manifest.get("config")
    if (
        not isinstance(config, dict)
        or digest(config) != manifest.get("config_sha256")
        or digest(config.get("definition")) != digest(definition)
        or manifest.get("input_sha256") != digest(inputs)
        or manifest.get("definition_sha256") != digest(definition)
        or manifest.get("records") != len(packets)
        or digest(read(output / "inputs.json")) != digest(inputs)
    ):
        raise ValueError("Frozen manifest/config/inputs/definition mismatch")
    if (
        summary.get("state") != "completed"
        or type(summary.get("processed")) is not int
        or type(summary.get("expected")) is not int
        or summary.get("processed") != len(packets)
        or summary.get("expected") != len(packets)
        or summary.get("config_sha256") != manifest["config_sha256"]
    ):
        raise ValueError("Model run incomplete or summary identity mismatch")
    if {p.name for p in output.glob("result-*.json")} != {
        f"result-{ident}.json" for ident in packets
    }:
        raise ValueError("Missing or unexpected model result files")
    contract = read(base / "target-contract-verified.json")
    if (
        contract.get("status") != "source_verified_runtime_unvalidated"
        or contract.get("submit_ready") is not False
        or contract.get("verification", {}).get("target_files_written") is not False
        or not contract.get("verification", {}).get("source_hashes")
    ):
        raise ValueError(
            "Expected verified read-only target source contract, not import readiness"
        )
    references, review_map = checked_reviews(
        reviews,
        [base / f"astra-reference-part{i}.json" for i in (1, 2)],
        set(packets),
        sha(base / "definition.json"),
    )
    index = indexes(read(standards_path))
    rows, results = [], []
    for packet in packets.values():
        preflight = validate_candidate(
            packet, {"candidate": None, "reason": "Input provenance preflight only."}
        )
        if not preflight["valid"]:
            raise ValueError(f"Frozen packet provenance invalid: {packet['id']}")
        ident = packet["id"]
        result_path = output / f"result-{ident}.json"
        result = read(result_path)
        checked_result(result, packet, manifest)
        results.append(result)
        ref, review = references[ident], review_map[ident]
        if (
            ref.get("input_sha256") != packet["input_sha256"]
            or review.get("input_sha256") != packet["input_sha256"]
            or review.get("candidate_sha256") != sha(result_path)
        ):
            raise ValueError("Reference/review record identity changed")
        validation = validate_candidate(packet, result.get("payload"))
        ref_validation = validate_candidate(packet, ref.get("payload"))
        payload = result.get("payload")
        candidate = payload.get("candidate") if isinstance(payload, dict) else None
        normalized = normalized_entities(candidate, validation, index)
        rows.append(
            {
                "id": ident,
                "article": packet["article"],
                "input_sha256": packet["input_sha256"],
                "candidate": candidate,
                "model_status": result["status"],
                "seconds": result["seconds"],
                "model_result_sha256": sha(result_path),
                "validation": validation,
                "normalized_entities": normalized,
                "reference": ref,
                "reference_validation": ref_validation,
                "review": review,
                "mapping": mapping(packet, candidate, normalized, validation, review),
            }
        )
    # Recompute aggregates from checkpoint records, never trust a stale summary.
    status_counts = dict(Counter(r["status"] for r in results))
    total_seconds = round(sum(r["seconds"] for r in results), 6)
    for name, value in {
        "statuses": status_counts,
        "input_tokens": sum(r["input_tokens"] for r in results),
        "output_tokens": sum(r["output_tokens"] for r in results),
    }.items():
        if digest(summary.get(name)) != digest(value):
            raise ValueError(f"Model summary differs from results: {name}")
    for name, value in {
        "total_inference_seconds": total_seconds,
        "mean_seconds": round(total_seconds / len(rows), 6),
    }.items():
        if type(summary.get(name)) not in (int, float) or not math.isclose(
            summary[name], value, abs_tol=0.00001
        ):
            raise ValueError(f"Model summary differs from results: {name}")
    entities = [e for row in rows for e in row["normalized_entities"]]
    raw_entities = [
        e
        for row in rows
        if isinstance(row["candidate"], dict)
        and isinstance(row["candidate"].get("entities"), list)
        for e in row["candidate"]["entities"]
    ]
    metrics = {
        "packets": len(rows),
        "documents": len({row["article"]["pmid"] for row in rows}),
        "model_statuses": status_counts,
        "schema_and_provenance_pass": sum(
            row["validation"]["status"] == "structurally_valid_candidate"
            for row in rows
        ),
        "abstentions": sum(row["validation"]["status"] == "abstained" for row in rows),
        "reference_schema_and_provenance_pass": sum(
            row["reference_validation"]["status"] == "structurally_valid_candidate"
            for row in rows
        ),
        "semantic_verdicts": dict(Counter(row["review"]["verdict"] for row in rows)),
        "joint_pass": sum(
            row["validation"]["status"] == "structurally_valid_candidate"
            and row["review"]["verdict"] == "supported"
            for row in rows
        ),
        "entity_exact_span_count": sum(
            len(row["validation"]["entity_spans"]) for row in rows
        ),
        "evidence_quotes": sum(
            len(row["candidate"]["evidence"])
            for row in rows
            if isinstance(row["candidate"], dict)
            and isinstance(row["candidate"].get("evidence"), list)
        ),
        "evidence_exact_span_count": sum(
            len(row["validation"]["evidence_spans"]) for row in rows
        ),
        "entity_mentions": len(raw_entities),
        "source_anchored_entity_mentions": sum(e["source_anchored"] for e in entities),
        "unique_exact_alias_links": sum(
            e["normalization_status"] == "exact_alias_unique" for e in entities
        ),
        "mapping_api_ready": 0,
        "validation_error_codes": dict(
            Counter(e["code"] for row in rows for e in row["validation"]["errors"])
        ),
        "semantic_error_categories": dict(
            Counter(e["category"] for row in rows for e in row["review"]["errors"])
        ),
    }
    return {
        "version": VERSION,
        "metrics": metrics,
        "timing": summary,
        "records": rows,
        "sources": {
            "model_output": str(output.resolve()),
            "standards": str(standards_path.resolve()),
            "reviews": [str(p.resolve()) for p in reviews],
        },
        "identity": {
            "inputs_sha256": sha(base / "inputs.json"),
            "definition_sha256": sha(base / "definition.json"),
            "manifest_sha256": sha(output / "manifest.json"),
            "summary_sha256": sha(output / "summary.json"),
            "target_contract_sha256": sha(base / "target-contract-verified.json"),
            "reference_sha256": [
                sha(base / f"astra-reference-part{i}.json") for i in (1, 2)
            ],
            "review_sha256": [sha(p) for p in reviews],
            "standards_sha256": sha(standards_path),
        },
        "limitations": [
            "目的性开发片段组；不是全篇或全库抽取召回率。",
            "Astra为模型参考，未做人工金标准或独立标注者一致性测量。",
            "本次联合抽取只有一个基线；没有实测拆分调用的提升。",
            "标准实体精确别名候选与目标内部UUID分开；未调用tumor_api接口或数据库。",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--standards", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    evaluation = build(args.base, args.model_output, args.standards, args.reviews)
    freeze(args.base / "evaluation.json", evaluation)
    print(json.dumps(evaluation["metrics"], ensure_ascii=False))
