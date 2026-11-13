"""Prepare a read-only comparison of cached PubTator and existing 9B outputs.

No network, model inference, or database access. Source references and judgments
are supplied separately; an entity count is never interpreted as accuracy.
"""

import argparse
import difflib
import json
import unicodedata
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def fold(text):
    return "".join(
        ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum()
    )


def best_passage(text, passages):
    candidates = []
    for index, passage in enumerate(passages):
        matcher = difflib.SequenceMatcher(
            None, fold(text), fold(passage.get("text", "")), autojunk=False
        )
        if matcher.quick_ratio() >= 0.65:
            candidates.append((matcher.ratio(), index))
    if not candidates:
        return 0.0, None
    score, index = max(candidates)
    return score, index


def prepare(batch, pubtator, samples_per_article):
    batch = Path(batch)
    packets = read(batch / "grouped/packets.json")["records"]
    docs = {
        str(d["pmid"]): d for d in read(pubtator)["PubTator3"] if d.get("pmcid")
    }
    rows = []
    for pmid, doc in sorted(docs.items()):
        eligible = [r for r in packets if r["article_pmid"] == pmid]
        if len(eligible) < samples_per_article:
            raise ValueError(f"Insufficient source groups: {pmid}")
        indices = [
            round(i * (len(eligible) - 1) / (samples_per_article - 1))
            for i in range(samples_per_article)
        ]
        for index in indices:
            packet = eligible[index]
            anchor = next(
                p for p in packet["passages"] if p["id"] == packet["anchor_id"]
            )
            ratio, passage_index = best_passage(anchor["text"], doc["passages"])
            rows.append(
                {
                    "id": packet["id"],
                    "pmid": pmid,
                    "article": packet["article"],
                    "eligible_groups": len(eligible),
                    "selected_index": index,
                    "anchor": anchor,
                    "pubtator_alignment": {
                        "ratio": ratio,
                        "passage_index": passage_index,
                        "eligible": ratio >= 0.98,
                        "method": "NFKC-casefold-alphanumeric SequenceMatcher",
                    },
                }
            )
    return {
        "schema": "pubtator-entity-comparison-v1",
        "selection": "five evenly spaced group indices per fulltext-covered article",
        "scope": "anchor passage only; original model context differs",
        "records": rows,
    }


def predictions(selection, batch, pubtator):
    batch = Path(batch)
    assembled = {
        r["id"]: r
        for r in read(batch / "stages/entities/assembled-packets.json")["records"]
    }
    docs = {str(d["pmid"]): d for d in read(pubtator)["PubTator3"]}
    output = []
    for sample in selection["records"]:
        rid, anchor = sample["id"], sample["anchor"]
        record = assembled[rid]
        qwen = []
        for entity in record.get("entities", []):
            locations = [
                loc for loc in entity.get("locations", [])
                if loc["passage_id"] == anchor["id"]
            ]
            if not locations:
                continue
            for loc in locations:
                quote = anchor["text"][loc["start"]:loc["end"]]
                if fold(quote) != fold(entity["text"]):
                    raise ValueError(f"Invalid baseline location: {rid}/{entity['id']}")
            qwen.append({
                "id": entity["id"], "type": entity["type"],
                "text": entity["text"], "locations": locations,
                "link": entity.get("link", {}),
            })
        alignment = sample["pubtator_alignment"]
        pt, rejected = [], []
        if alignment["eligible"]:
            passage = docs[sample["pmid"]]["passages"][alignment["passage_index"]]
            seen = {}
            for entity in passage.get("annotations", []):
                for loc in entity["locations"]:
                    start = loc["offset"] - passage["offset"]
                    if passage["text"][start:start + loc["length"]] != entity["text"]:
                        raise ValueError(f"Invalid PubTator location: {rid}")
                if not fold(entity["text"]) or fold(entity["text"]) not in fold(anchor["text"]):
                    rejected.append(entity)
                    continue
                info = entity.get("infons", {})
                key = (info.get("type"), entity["text"], str(info.get("identifier")))
                if key in seen:
                    seen[key]["annotation_ids"].append(entity["id"])
                    continue
                item = {
                    "id": f"{rid}.pt{len(pt) + 1}",
                    "type": info.get("type"), "text": entity["text"],
                    "infons": info, "annotation_ids": [entity["id"]],
                }
                pt.append(item)
                seen[key] = item
        raw = read(batch / f"stages/entities/extract/result-{rid}.json")
        output.append({
            "id": rid, "qwen9b": qwen, "pubtator": pt,
            "projection_rejected": rejected,
            "qwen_execution": record["entity_stage"],
            "qwen_seconds": raw.get("seconds"),
            "qwen_input_tokens": raw.get("input_tokens"),
            "qwen_output_tokens": raw.get("output_tokens"),
        })
    return {"schema": "pubtator-comparison-predictions-v1", "records": output}


def packet_predictions(selection, batch, pubtator):
    """Keep the original 9B context for both arms, including cancer aliases."""
    packets = {r["id"]: r for r in read(Path(batch) / "grouped/packets.json")["records"]}
    docs = {str(d["pmid"]): d for d in read(pubtator)["PubTator3"]}
    cache, output = {}, []
    for sample in selection["records"]:
        packet = packets[sample["id"]]
        projections, alignments = [], []
        for passage in packet["passages"]:
            key = (sample["pmid"], passage["id"])
            if key not in cache:
                cache[key] = best_passage(passage["text"], docs[sample["pmid"]]["passages"])
            ratio, index = cache[key]
            alignment = {"ratio": ratio, "passage_index": index, "eligible": ratio >= 0.98}
            alignments.append({"source_passage_id": passage["id"], **alignment})
            if not alignment["eligible"]:
                continue
            projections.append({**sample, "anchor": passage, "pubtator_alignment": alignment})
        if len(projections) != len(packet["passages"]):
            raise ValueError(f"Incomplete common context for {sample['id']}: {alignments}")
        pieces = predictions({"records": projections}, batch, pubtator)["records"]
        merged = {**pieces[0], "qwen9b": [], "pubtator": [], "projection_rejected": []}
        merged["common_passages"] = alignments
        for arm in ("qwen9b", "pubtator"):
            seen = {}
            for projection, piece in zip(projections, pieces, strict=True):
                for entity in piece[arm]:
                    key = (entity["type"], entity["text"], str(entity.get("infons", {}).get("identifier")))
                    if key in seen:
                        seen[key]["source_passage_ids"].append(projection["anchor"]["id"])
                        continue
                    item = {**entity, "source_passage_ids": [projection["anchor"]["id"]]}
                    if arm == "pubtator":
                        item["id"] = f"{sample['id']}.pt{len(merged[arm]) + 1}"
                    merged[arm].append(item)
                    seen[key] = item
        merged["projection_rejected"] = [e for p in pieces for e in p["projection_rejected"]]
        output.append(merged)
    return {"schema": "pubtator-comparison-packet-predictions-v1", "records": output}


def name_key(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def hybrid(predicted, standards):
    """An additive research arm; no gold/reference labels influence admission.

    Disease mappings are never adopted. Genes require agreement with the frozen
    local dictionary. Chemicals require an existing same-name drug/treatment
    and a normalized preferred name matching a local RxNorm ingredient.
    """
    standards = read(standards)
    genes, ingredients = {}, {}
    for gene in standards["genes"]:
        for alias in [gene["symbol"], gene.get("name", ""), *gene.get("synonyms", [])]:
            genes.setdefault(name_key(alias), set()).add(str(gene["gene_id"]))
    for concept in standards["concepts"]:
        if standards["releases"][concept["release_id"]]["namespace"].upper() != "RXNORM":
            continue
        ingredients.setdefault(name_key(concept["label"]), set()).add(concept["identifier"])
    output = []
    for row in predicted["records"]:
        baseline = row["qwen9b"]
        combined, additions, rejected = list(baseline), [], []
        for candidate in row["pubtator"]:
            info, kind = candidate["infons"], candidate["type"]
            name = name_key(candidate["text"])
            identifier = str(info.get("identifier", ""))
            target_kind, target_id = None, None
            if kind == "Gene" and identifier.isdecimal():
                if genes.get(name) == {identifier}:
                    target_kind, target_id = "gene", f"NCBIGene:{identifier}"
            elif kind == "Chemical" and identifier.startswith("MESH:"):
                same = [e for e in baseline if name_key(e["text"]) == name and e["type"] in {"drug", "treatment"}]
                local = ingredients.get(name_key(info.get("name", "")), set())
                if same and len(local) == 1:
                    target_kind, target_id = "drug", next(iter(local))
            if target_kind is None:
                rejected.append({"pubtator_id": candidate["id"], "reason": "outside_additive_rule_or_dictionary_disagreement"})
                continue
            same_typed = [e for e in combined if name_key(e["text"]) == name and e["type"] in ({"gene", "protein"} if target_kind == "gene" else {"drug"})]
            if same_typed:
                continue
            item = {
                **candidate, "id": candidate["id"] + ".hybrid", "type": target_kind,
                "link": {"candidate_id": target_id, "method": "pubtator_and_frozen_local_dictionary"},
                "origin": "pubtator_dictionary_addition", "relation_role": "not_inferred",
            }
            combined.append(item)
            additions.append(item)
        output.append({"id": row["id"], "hybrid": combined, "additions": additions, "rejected": rejected})
    return {"schema": "pubtator-comparison-hybrid-v1", "records": output}


def evaluate(reference, predicted, combined, policy):
    """Score only the explicitly reviewed core identities, not all NER output."""
    expected = {r["id"]: r for r in reference["records"]}
    predictions_by_id = {r["id"]: r for r in predicted["records"]}
    hybrids_by_id = {r["id"]: r for r in combined["records"]}
    if set(expected) != set(predictions_by_id) or set(expected) != set(hybrids_by_id):
        raise ValueError("Incomplete comparison arms")
    kinds = {"cancer": {"disease", "Disease"}, "drug": {"drug", "Chemical"},
             "gene": {"gene", "protein", "Gene"}, "variant": {"variant", "Variant"}}
    rows = []
    for rid, ref in expected.items():
        arms = {arm: predictions_by_id[rid][arm] for arm in ("qwen9b", "pubtator")}
        arms["hybrid"] = hybrids_by_id[rid]["hybrid"]
        evaluated = {}
        for arm, entities in arms.items():
            scores = []
            for target in ref["required"]:
                aliases = [fold(s) for s in policy["accepted_names"][target["key"]]]
                matches = []
                for entity in entities:
                    label = fold(entity["text"])
                    match = any(a in label for a in aliases) if target["kind"] == "cancer" else label in aliases
                    if match:
                        matches.append(entity)
                typed = [e for e in matches if e["type"] in kinds[target["kind"]]]
                scores.append({
                    "key": target["key"], "kind": target["kind"],
                    "identity_present": bool(matches), "typed_entity_present": bool(typed),
                    "matching_ids": [e["id"] for e in matches],
                    "typed_matching_ids": [e["id"] for e in typed],
                })
            evaluated[arm] = scores
        rows.append({"id": rid, "arms": evaluated})
    metrics = {}
    for arm in ("qwen9b", "pubtator", "hybrid"):
        targets = [t for r in rows for t in r["arms"][arm]]
        positive = [r["arms"][arm] for r in rows if r["arms"][arm]]
        by_kind = {}
        for kind in sorted({t["kind"] for t in targets}):
            subset = [t for t in targets if t["kind"] == kind]
            by_kind[kind] = {"total": len(subset),
                             "identity_present": sum(t["identity_present"] for t in subset),
                             "typed_entity_present": sum(t["typed_entity_present"] for t in subset)}
        metrics[arm] = {
            "required_identities": len(targets),
            "identity_present": sum(t["identity_present"] for t in targets),
            "typed_entity_present": sum(t["typed_entity_present"] for t in targets),
            "groups_with_core_targets": len(positive),
            "identity_complete_groups": sum(all(t["identity_present"] for t in group) for group in positive),
            "typed_complete_groups": sum(all(t["typed_entity_present"] for t in group) for group in positive),
            "by_kind": by_kind,
        }
    return {"schema": "pubtator-comparison-core-evaluation-v1", "metrics": metrics,
            "standard_id_accuracy": "separate_review_not_in_core_score",
            "whole_knowledge_accuracy": "not_measured", "records": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "predictions", "packet-predictions", "hybrid", "evaluate"])
    parser.add_argument("--batch", required=True)
    parser.add_argument("--pubtator", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples-per-article", type=int, default=5)
    parser.add_argument("--selection")
    parser.add_argument("--predictions")
    parser.add_argument("--standards")
    parser.add_argument("--reference")
    parser.add_argument("--hybrid")
    parser.add_argument("--policy")
    args = parser.parse_args()
    if args.samples_per_article < 2:
        parser.error("--samples-per-article must be at least 2")
    if args.phase == "prepare":
        result = prepare(args.batch, args.pubtator, args.samples_per_article)
    elif args.phase == "hybrid":
        if not args.predictions or not args.standards:
            parser.error("hybrid requires --predictions and --standards")
        result = hybrid(read(args.predictions), args.standards)
    elif args.phase == "evaluate":
        if not all([args.reference, args.predictions, args.hybrid, args.policy]):
            parser.error("evaluate requires --reference, --predictions, --hybrid and --policy")
        result = evaluate(read(args.reference), read(args.predictions), read(args.hybrid), read(args.policy))
    else:
        if not args.selection:
            parser.error("predictions requires --selection")
        function = packet_predictions if args.phase == "packet-predictions" else predictions
        result = function(read(args.selection), args.batch, args.pubtator)
    target = Path(args.output)
    if target.exists():
        if read(target) != result:
            raise ValueError(f"Refusing to replace existing research result: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"path": str(target), "records": len(result["records"])}))


if __name__ == "__main__":
    main()
