"""Compare core-intake reviews and export source-preserving research knowledge."""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.core_knowledge_intake import assemble
from apps.curation.core_knowledge_projection import VERSION as PROJECTION_VERSION
from apps.curation.core_knowledge_projection import project
from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha

BASE = Path("var/experiments/relation-facts-20261029")
UPSTREAM = Path("var/experiments/lean-entities-20261029/evaluation.json")
STATE = "research_core_supported_not_production"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def indexed(rows, label):
    result = {r["id"]: r for r in rows}
    require(len(result) == len(rows), f"Duplicate IDs in {label}")
    return result


def check_review(review, packet):
    for field in (
        "core_usable",
        "clinical_core_supported",
        "anchor_focused",
        "literal_drugs",
    ):
        require(
            type(review.get(field)) is bool, f"Missing boolean {field}: {packet['id']}"
        )
    require(isinstance(review.get("blocking"), list), "Blocking errors must be a list")
    require(isinstance(review.get("deferred"), list), "Deferred details must be a list")
    require(
        all(isinstance(x, str) for x in review["deferred"]),
        "Deferred details must be text",
    )
    require(
        bool(review.get("brief")) and bool(review.get("drug_fidelity_status")),
        "Missing review explanation",
    )
    if review["core_usable"]:
        require(
            not review["blocking"]
            and review["clinical_core_supported"]
            and review["literal_drugs"],
            "Unsupported core pass",
        )
    passages = {
        key: p["text"] for p in packet["passages"] for key in (p["id"], p["alias"])
    }
    for error in review["blocking"]:
        require(
            error.get("category") and error.get("problem"), "Unexplained blocking error"
        )
        pid, quote = error.get("passage_id"), error.get("quote")
        if (
            not pid
            and not quote
            and error["category"]
            in {"protocol", "structure", "parse", "format", "json"}
        ):
            continue
        require(
            pid in passages
            and isinstance(quote, str)
            and bool(quote)
            and quote in passages[pid],
            f"Unverified review quote: {packet['id']}",
        )


def scores(reviews):
    fields = (
        "core_usable",
        "clinical_core_supported",
        "anchor_focused",
        "literal_drugs",
    )
    return {
        "groups": len(reviews),
        **{k: sum(r[k] for r in reviews) for k in fields},
        "core_and_anchor": sum(
            r["core_usable"] and r["anchor_focused"] for r in reviews
        ),
        "core_off_anchor": sum(
            r["core_usable"] and not r["anchor_focused"] for r in reviews
        ),
        "with_deferred_details": sum(bool(r["deferred"]) for r in reviews),
        "semantic_overlap_marked": sum(bool(r.get("duplicate_of")) for r in reviews),
        "drug_fidelity_statuses": dict(
            Counter(r["drug_fidelity_status"] for r in reviews)
        ),
        "blocking_categories": dict(
            Counter(e["category"] for r in reviews for e in r["blocking"])
        ),
    }


def exact_key(candidate):
    fact = deepcopy(candidate["fact"])
    fact.pop("entity_ids", None)
    fact.pop("evidence", None)
    for result in fact["results"]:
        result.pop("passage", None)
    article = candidate["article"]
    identity = {
        "article": [article["pmid"], article["version_id"], article["source_sha256"]],
        "fact": fact,
        "entities": sorted(
            (e.get("type"), e.get("text")) for e in candidate["entities"]
        ),
    }
    return json.dumps(identity, ensure_ascii=False, sort_keys=True)


def knowledge_items(rows, packets):
    items, by_key, merged = [], {}, []
    for row in rows:
        if not row["ready_for_export"]:
            continue
        candidate, rid = row["assembly"]["candidate"], row["id"]
        projection = row["projection"]
        core_fact = deepcopy(candidate["fact"])
        core_fact["drug_names"] = [d["name"] for d in projection["drugs"]]
        key = exact_key(candidate)
        source = {
            "group_id": rid,
            "result_sha256": row["result_sha256"],
            "raw_model_fact": deepcopy(row["raw_model_fact"]),
            "raw_drug_names": deepcopy(row["raw_model_fact"].get("drug_names")),
            "assembled_candidate": deepcopy(candidate),
            "cited_evidence": deepcopy(candidate["evidence"]),
            "original_context": deepcopy(packets[rid]["passages"]),
            "original_entities": deepcopy(packets[rid]["entities"]),
            "projection": deepcopy(projection),
            "reference_review": deepcopy(row["review"]),
        }
        if key in by_key:
            item = by_key[key]
            item["source_groups"].append(source)
            item["deferred_details"] = list(
                dict.fromkeys(item["deferred_details"] + row["review"]["deferred"])
            )
            merged.append(
                {
                    "group_id": rid,
                    "into": item["id"],
                    "reason": "literal_equal_same_article_core_fact",
                }
            )
            continue
        item = {
            "id": f"core-knowledge-20261029:{rid}",
            "state": STATE,
            "article": deepcopy(candidate["article"]),
            "raw_model_fact": deepcopy(row["raw_model_fact"]),
            "raw_drug_names": deepcopy(row["raw_model_fact"].get("drug_names")),
            "core_fact": core_fact,
            "entities": deepcopy(candidate["entities"]),
            "drugs": deepcopy(projection["drugs"]),
            "regimen_mentions": deepcopy(projection["regimen_mentions"]),
            "other_source_terms": deepcopy(projection["other_source_terms"]),
            "unresolved_terms": deepcopy(projection["unresolved_terms"]),
            "projection_version": PROJECTION_VERSION,
            "script_projection_changes": deepcopy(projection["changes"]),
            "business_disease_links": deepcopy(candidate["business_disease_links"]),
            "source_groups": [source],
            "review_model": "gpt-6-astra",
            "deferred_details": deepcopy(row["review"]["deferred"]),
            "semantic_overlap_hint": row["review"].get("duplicate_of"),
            "notes": "Astra reviews the raw model result. Typed terms and core_fact.drug_names are a script projection using existing type hints and literal locations, not a new medical verification. Standard IDs remain candidates; regimen relationships are not inferred. Not a production import.",
        }
        by_key[key] = item
        items.append(item)
    return items, merged


def build(root):
    reference, reviewed = read(root / "reference.json"), read(root / "review.json")
    require(
        reference["model"] == reviewed["model"] == "gpt-6-astra",
        "Expected actual Astra review",
    )
    require(
        reviewed["reference_sha256"] == sha(root / "reference.json"),
        "Reference changed",
    )
    packets_doc = read(root / "packets.json")
    require(
        packets_doc == read(BASE / "packets.json"),
        "Original paragraphs/entities changed",
    )
    for name, digest in reference["source_sha256"].items():
        require(sha(BASE / name) == digest, f"Reviewed 011 source changed: {name}")
    packets = indexed(packets_doc["records"], "packets")
    refs = indexed(reference["records"], "reference")
    reviews = indexed(reviewed["records"], "review")
    stored = indexed(read(root / "assembled.json")["records"], "assembled")
    require(
        set(packets)
        == set(refs)
        == set(reviews)
        == set(stored)
        == {f"K{i:02}" for i in range(1, 16)},
        "Need same 15 groups",
    )
    rows, output_paths = [], []
    for rid, packet in sorted(packets.items()):
        path = root / "extract" / f"result-{rid}.json"
        raw, review = read(path), reviews[rid]
        require(
            raw["id"] == rid
            and review["result_sha256"] == stored[rid]["result_sha256"] == sha(path),
            f"Prediction changed: {rid}",
        )
        if raw["status"] == "classified":
            require(
                parse(raw["raw_output"]) == raw["payload"], f"Raw JSON differs: {rid}"
            )
        else:
            require(raw["payload"] is None, "No partial JSON rescue")
        actual = assemble(packet, raw)
        require(
            all(stored[rid][k] == v for k, v in actual.items()),
            f"Assembly changed: {rid}",
        )
        check_review(review, packet)
        require(
            not review["core_usable"] or raw["status"] == "classified",
            "Failed output cannot pass core",
        )
        projection = (
            project(actual["candidate"], packet) if actual["candidate"] else None
        )
        technical_ready = actual["structure_status"] == "core_structurally_valid"
        ready = review["core_usable"] and technical_ready
        rows.append(
            {
                "id": rid,
                "result_sha256": sha(path),
                "ready_for_export": ready,
                "raw_semantic_core_usable": review["core_usable"],
                "technical_ready_for_export": technical_ready,
                "raw_model_fact": deepcopy((raw.get("payload") or {}).get("fact")),
                "projection_version": PROJECTION_VERSION,
                "projection": projection,
                "old_review": refs[rid]["old_review"],
                "review": review,
                "assembly": actual,
                "inference_seconds": raw["seconds"],
                "script_seconds": stored[rid]["script_seconds"],
            }
        )
        output_paths.append(path)
    summary = read(root / "extract" / "summary.json")
    require(
        summary["state"] == "completed" and summary["processed"] == 15, "Incomplete run"
    )
    inference, script = (
        sum(r[k] for r in rows) for k in ("inference_seconds", "script_seconds")
    )
    require(
        abs(inference - summary["total_inference_seconds"]) < 0.00001,
        "Inference sum differs",
    )
    items, merges = knowledge_items(rows, packets)
    upstream = read(UPSTREAM)["timing"]["010"]
    old_time = read(BASE / "evaluation.json")["timing"]["relation"]
    sessions = summary["sessions"]
    paths = output_paths + [
        root / n
        for n in (
            "reference.json",
            "review.json",
            "packets.json",
            "definition.json",
            "assembled.json",
            "plan.json",
            "extract/summary.json",
            "extract/manifest.json",
        )
    ]
    paths += [
        BASE / "evaluation.json",
        UPSTREAM,
        Path(__file__),
        Path("apps/curation/core_knowledge_intake.py"),
        Path("apps/curation/core_knowledge_projection.py"),
    ]
    evaluation = {
        "version": "core-knowledge-evaluation-v2",
        "projection_version": PROJECTION_VERSION,
        "policy_version": reference["policy_version"],
        "review_kind": "gpt-6-astra_model_reference_not_human_gold",
        "scores": {
            "old_same_policy": scores([r["old_review"] for r in rows]),
            "new_same_policy": scores([r["review"] for r in rows]),
            "new_structure_valid": sum(
                r["assembly"]["structure_status"] == "core_structurally_valid"
                for r in rows
            ),
            "new_ready_for_export": sum(r["ready_for_export"] for r in rows),
            "exported_after_exact_merge": len(items),
        },
        "exact_merges": merges,
        "records": rows,
        "timing": {
            "task_model_calls": 15,
            "inference_seconds": inference,
            "script_seconds": script,
            "processing_seconds": inference + script,
            "old_011_relation": old_time,
            "upstream_010_processing_seconds": upstream["processing_seconds"],
            "cumulative_processing_seconds": upstream["processing_seconds"]
            + inference
            + script,
            "model_load_seconds": sum(s.get("load_seconds", 0) for s in sessions),
            "warmup_seconds": sum(s.get("warmup_seconds", 0) for s in sessions),
            "warmup_calls": sum("warmup_seconds" in s for s in sessions),
            "runner_wall_seconds": sum(s["wall_seconds"] for s in sessions),
            "peak_process_mib": summary.get("nvml_process_peak_mib"),
            "input_tokens": summary["input_tokens"],
            "output_tokens": summary["output_tokens"],
        },
        "limits": [
            "Paired content scores use the same business policy; original 011 strict scores unchanged.",
            "012 adds drug_names; new mechanical structure is reported separately, not imposed on old output.",
            "Raw semantic eligibility is Astra's unchanged review; technical readiness checks structure only. Typed exports use a separate literal-source projection, preserving unknown/class terms without inferring drugs or regimen relationships.",
            "15 groups are not 15 independent facts. Semantic overlap hints are retained, not automatically merged.",
            "Astra research review and human labor excluded from inference timing; no production validation or training.",
        ],
        "sources": [{"path": str(p), "sha256": sha(p)} for p in paths],
    }
    return evaluation, {
        "version": "research-core-knowledge-items-v2",
        "projection_version": PROJECTION_VERSION,
        "state": STATE,
        "records": items,
        "exact_merges": merges,
    }


def freeze_jsonl(path, records):
    data = "".join(
        json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in records
    )
    if path.exists():
        require(path.read_text() == data, f"Immutable JSONL differs: {path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=".knowledge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, path)
        except FileExistsError:
            require(path.read_text() == data, "Concurrent JSONL differs")
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    evaluation, exported = build(args.root)
    freeze(args.root / "evaluation.json", evaluation)
    exported["evaluation_sha256"] = sha(args.root / "evaluation.json")
    freeze(args.root / "knowledge-items.json", exported)
    freeze_jsonl(args.root / "knowledge-items.jsonl", exported["records"])
    print(json.dumps(evaluation["scores"], ensure_ascii=False))
