"""Aggregate the frozen 011 Astra review without repairing model output."""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_knowledge_candidates import freeze, parse, read, sha
from scripts.relation_fact_pilot import BASE, assemble_one, text

PROTOCOL_ERRORS = {"protocol", "structure", "schema", "parse", "json", "format"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def indexed(rows, label):
    result = {row["id"]: row for row in rows}
    require(len(result) == len(rows), f"Duplicate IDs in {label}")
    return result


def check_quote(item, packet, *, optional=False):
    pid, quote = item.get("passage_id"), item.get("quote")
    if optional and not pid and not quote:
        return
    passages = {p["id"]: p["text"] for p in packet["passages"]}
    require(
        pid in passages
        and isinstance(quote, str)
        and bool(quote.strip())
        and quote in passages[pid],
        f"Unverified review quotation in {packet['id']}: {pid}",
    )


def build(root):
    policy = read(root / "evaluation-policy.json")
    packets = indexed(read(root / "packets.json")["records"], "packets")
    expected = {f"K{i:02}" for i in range(1, 16)}
    require(policy["denominator"] == 15 and set(packets) == expected, "Need 15 groups")
    references, reviews, source_paths = [], [], []
    for suffix in ("a", "b"):
        ref_path = root / f"astra-reference-{suffix}.json"
        rev_path = root / f"astra-review-{suffix}.json"
        for path, target in ((ref_path, references), (rev_path, reviews)):
            document = read(path)
            require(
                document.get("model", document.get("reviewer_model")) == "gpt-6-astra",
                f"Expected actual Astra attribution: {path}",
            )
            if path == rev_path:
                require(
                    document.get("reference_sha256") == sha(ref_path),
                    f"Review reference hash mismatch: {path}",
                )
                require(
                    {r["id"] for r in document["records"]}
                    <= {r["id"] for r in read(ref_path)["records"]},
                    f"Review uses another reference part: {path}",
                )
            target.extend(document["records"])
            source_paths.append(path)
    for source in policy["references"]:
        require(sha(Path(source["path"])) == source["sha256"], "Reference changed")
    references, reviews = indexed(references, "references"), indexed(reviews, "reviews")
    assembled = indexed(read(root / "assembled.json")["records"], "assembly")
    require(
        set(references) == set(reviews) == set(assembled) == expected,
        "Coverage mismatch",
    )
    axes = {axis: Counter() for axis in policy["axes"]}
    scores = Counter(denominator=15)
    categories, rows = {}, []
    inference_seconds = script_seconds = 0.0
    for rid in sorted(packets):
        packet, review, stored = packets[rid], reviews[rid], assembled[rid]
        result_path = root / "extract" / f"result-{rid}.json"
        raw = read(result_path)
        require(raw["id"] == rid, f"Result ID mismatch: {rid}")
        require(
            review["result_sha256"] == stored["result_sha256"] == sha(result_path),
            f"Reviewed result changed: {rid}",
        )
        rebuilt = assemble_one(packet, raw)
        require(
            all(stored[k] == rebuilt[k] for k in rebuilt), f"Assembly changed: {rid}"
        )
        json_ok = raw["status"] == "classified"
        if json_ok:
            require(isinstance(raw["payload"], dict), f"Missing JSON object: {rid}")
            require(
                parse(raw["raw_output"]) == raw["payload"], f"Raw JSON changed: {rid}"
            )
        else:
            require(raw["payload"] is None, f"Failure has rescued payload: {rid}")
        verdicts = review["axes"]
        require(set(verdicts) == set(axes), f"Review axis coverage mismatch: {rid}")
        require(
            set(verdicts.values()) <= set(policy["axis_values"]), f"Unknown axis: {rid}"
        )
        for axis, verdict in verdicts.items():
            axes[axis][verdict] += 1
        errors = review["errors"]
        require(isinstance(errors, list), f"Errors must be a list: {rid}")
        for error in errors:
            require(error["severity"] in {"blocking", "minor"}, f"Bad severity: {rid}")
            require(
                all(
                    text(error.get(k))
                    for k in ("category", "field", "problem", "expected")
                ),
                f"Incomplete error explanation: {rid}",
            )
            check_quote(error, packet, optional=error["category"] in PROTOCOL_ERRORS)
            bucket = categories.setdefault(error["category"], Counter())
            bucket[error["severity"]] += 1
        for category in {e["category"] for e in errors}:
            categories[category]["groups"] += 1
        fact = raw["payload"].get("fact") if json_ok else None
        semantic = (
            isinstance(fact, dict)
            and all(v in {"pass", "not_applicable"} for v in verdicts.values())
            and verdicts["relation"] == verdicts["evidence_support"] == "pass"
            and not any(
                e["severity"] == "blocking" and e["category"] not in PROTOCOL_ERRORS
                for e in errors
            )
        )
        require(
            type(review["semantic_usable"]) is bool
            and review["semantic_usable"] == semantic,
            f"Semantic verdict conflicts: {rid}",
        )
        focuses = {f["key"] for f in references[rid]["acceptable_focuses"]}
        selected = review["reference_focus"]
        selected = {k.strip() for k in selected.split(";")} if text(selected) else set()
        novel = review["novel_focus_evidence"]
        # Empty/single evidence representations are adapted for checking only;
        # the original Astra record is preserved in the exported rows.
        novel = [] if novel is None else [novel] if isinstance(novel, dict) else novel
        require(isinstance(novel, list), f"Novel evidence must be a list: {rid}")
        for evidence in novel:
            check_quote(evidence, packet)
        if semantic and not (selected and selected <= focuses):
            require(bool(novel), f"Unknown focus lacks source evidence: {rid}")
            require(
                not any(e.get("accepted") is False for e in novel),
                f"Rejected novel focus cannot support semantic success: {rid}",
            )
        structural = rebuilt["status"] == "structurally_valid_candidate"
        usable = json_ok and structural and semantic
        for name, passed in {
            "json_valid": json_ok,
            "structure_valid": structural,
            "semantic_usable": semantic,
            "usable": usable,
            "abstained": rebuilt["status"] == "abstained",
            "json_failed": not json_ok,
        }.items():
            scores[name] += int(passed)
        rows.append(
            {
                "id": rid,
                "raw_status": raw["status"],
                "structure_status": rebuilt["status"],
                "structural_issues": rebuilt["issues"],
                "usable": usable,
                "statement": fact.get("statement") if isinstance(fact, dict) else None,
                **review,
            }
        )
        inference_seconds += raw["seconds"]
        script_seconds += stored["script_seconds"]
        source_paths.append(result_path)
    summary = read(root / "extract" / "summary.json")
    require(
        summary["state"] == "completed" and summary["processed"] == 15, "Incomplete run"
    )
    require(
        abs(inference_seconds - summary["total_inference_seconds"]) < 0.00001,
        "Inference time differs from checkpoints",
    )
    upstream = read(BASE / "evaluation.json")["timing"]["010"]
    sessions = summary["sessions"]
    relation = {
        "task_model_calls": 15,
        "inference_seconds": inference_seconds,
        "script_seconds": script_seconds,
        "processing_seconds": inference_seconds + script_seconds,
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "model_load_seconds": sum(s.get("load_seconds", 0) for s in sessions),
        "warmup_seconds": sum(s.get("warmup_seconds", 0) for s in sessions),
        "warmup_calls": sum("warmup_seconds" in s for s in sessions),
        "runner_wall_seconds": sum(s["wall_seconds"] for s in sessions),
        "peak_process_mib": summary.get("nvml_process_peak_mib"),
    }
    files = "evaluation-policy.json packets.json definition.json assembled.json extract/summary.json extract/manifest.json"
    source_paths += [root / name for name in files.split()] + [
        BASE / "evaluation.json",
        Path(__file__),
    ]
    return {
        "version": "relation-facts-evaluation-v1",
        "policy_version": policy["version"],
        "review_kind": "fresh_gpt-6-astra_reference_not_human_gold",
        "scores": dict(scores),
        "axes": {
            k: {v: counts[v] for v in policy["axis_values"]}
            for k, counts in axes.items()
        },
        "error_categories": {k: dict(v) for k, v in categories.items()},
        "records": rows,
        "timing": {
            "relation": relation,
            "upstream_010": upstream,
            "cumulative_processing_seconds": relation["processing_seconds"]
            + upstream["processing_seconds"],
            "cumulative_task_model_calls": 15 + upstream["task_model_calls"],
            "note": "Single-pass research; startups separate. Astra reference/review labor is excluded from pipeline speed; human correction time is not measured.",
        },
        "limits": [
            "Usable research candidates, not verified clinical applicability or production export approval.",
            "Different policy from 006/007 full-schema pass; do not compare those rates directly.",
            "Original failures and abstentions remain in the 15-attempt denominator; no repaired outputs.",
        ],
        "sources": [{"path": str(p), "sha256": sha(p)} for p in source_paths],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root)
    freeze(args.root / "evaluation.json", result)
    print(result["scores"])
