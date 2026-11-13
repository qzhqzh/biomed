"""Prepare source-only research inputs; no inference, publication or production writes."""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.knowledge_eligibility import anchor_eligibility
from scripts.core_knowledge_pilot import PROMPT as RELATION_PROMPT
from scripts.evaluate_knowledge_candidates import freeze, read, sha
from scripts.lean_entity_pilot import PROMPT as ENTITY_PROMPT
from scripts.light_entity_pilot import KINDS, passage_prompt, runner_row
from scripts.summary_ablation_pilot import remove_statement_prompt

VERSION = "source-only-knowledge-preparation-v1"


def relation_prompt(packet):
    """Exact 012/014 user-input layout, independent of input eligibility checks."""
    inventory = "\n".join(f"{e['id']} | {e['type']} | {e['text']}" for e in packet["entities"])
    support = "\n\n".join(
        f"[{p['alias']}] SUPPORT | {p['text']}" for p in packet["passages"] if p["role"] != "anchor"
    )
    anchor = "\n\n".join(
        f"[{p['alias']}] ANCHOR — THE FACT TO EXTRACT | {p['text']}" for p in packet["passages"] if p["role"] == "anchor"
    )
    return (
        f"TITLE (metadata only): {packet['article']['title']}\n\n"
        f"EXISTING ENTITY INVENTORY (hints only)\n{inventory}\n\n"
        f"BACKGROUND FOR NAMES AND CONTEXT\n{support}\n\n"
        f"CENTRAL EVIDENCE\n{anchor}"
    )


def require(value, message):
    if not value:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_packet(packet, stage):
    rid = packet.get("id")
    require(isinstance(rid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", rid), "Invalid packet ID")
    article, passages = packet.get("article"), packet.get("passages")
    require(isinstance(article, dict) and text(article.get("pmid")) and text(article.get("title")), f"{rid}: missing article PMID/title")
    require(isinstance(passages, list) and bool(passages), f"{rid}: missing original passages")
    seen, aliases, by_id = set(), set(), {}
    for passage in passages:
        require(isinstance(passage, dict), f"{rid}: invalid passage")
        pid = passage.get("id")
        require(text(pid) and pid not in seen, f"{rid}: duplicate/missing passage ID")
        seen.add(pid)
        by_id[pid] = passage
        require(passage.get("role") in {"anchor", "support"} and text(passage.get("text")), f"{rid}/{pid}: invalid original text/role")
        for field in ("locator", "version_id", "source_sha256", "parser_version"):
            require(text(passage.get(field)), f"{rid}/{pid}: missing provenance {field}")
        if stage == "relations":
            alias = passage.get("alias")
            require(isinstance(alias, str) and re.fullmatch(r"P[1-9][0-9]*", alias) and alias not in aliases,
                    f"{rid}/{pid}: invalid/duplicate P-number")
            aliases.add(alias)
    anchors = [p for p in passages if p["role"] == "anchor"]
    require(len(anchors) == 1 and packet.get("anchor_id") == anchors[0]["id"], f"{rid}: require exactly one matching ANCHOR")
    require(anchor_eligibility(anchors[0])["status"] == "eligible", f"{rid}: glossary_context_only; preserve as context, not an evidence center")
    if stage == "entities":
        require(packet.get("article_pmid", article["pmid"]) == article["pmid"], f"{rid}: inconsistent article PMID")
        return [], []
    entities = packet.get("entities")
    require(isinstance(entities, list) and bool(entities), f"{rid}: require assembled entity inventory")
    entity_ids, retained, deferred = set(), [], []
    for entity in entities:
        require(isinstance(entity, dict), f"{rid}: invalid entity object")
        eid = entity.get("id")
        require(text(eid) and eid not in entity_ids, f"{rid}: duplicate/missing entity ID")
        entity_ids.add(eid)
        require(entity.get("type") in KINDS and text(entity.get("text")), f"{rid}/{eid}: invalid entity type/text")
        locations = entity.get("locations")
        require(isinstance(locations, list), f"{rid}/{eid}: missing assembled location list")
        if not locations:
            deferred.append({"entity_id": eid, "reason": "not_located"})
            continue
        for location in locations:
            require(isinstance(location, dict), f"{rid}/{eid}: invalid source location")
            passage = by_id.get(location.get("passage_id"))
            start, end = location.get("start"), location.get("end")
            require(passage is not None and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(passage["text"])
                    and passage["text"][start:end] == entity["text"]
                    and location.get("role", passage["role"]) == passage["role"],
                    f"{rid}/{eid}: supplied location does not match original entity text")
        retained.append(entity)
    require(bool(retained), f"{rid}: no source-located entities; cannot prepare relations")
    return retained, deferred


def prepare(stage, packets_path, output):
    require(stage in {"entities", "relations"}, "Unknown extraction stage")
    packets_path, output = Path(packets_path), Path(output)
    dataset = read(packets_path)
    packets = dataset.get("records") if isinstance(dataset, dict) else None
    require(isinstance(packets, list) and bool(packets), "Expected nonempty packet records")
    ids, rows, contexts, deferred_all = set(), [], [], []
    for packet in packets:
        require(isinstance(packet, dict), "Invalid packet object")
        retained, deferred = validate_packet(packet, stage)
        rid = packet["id"]
        require(rid not in ids, f"Duplicate packet ID: {rid}")
        ids.add(rid)
        context = {key: packet[key] for key in ("id", "article", "anchor_id", "passages")}
        if stage == "entities":
            prompt = passage_prompt({**packet, "article_pmid": packet["article"]["pmid"]})
        else:
            prompt = relation_prompt({**packet, "entities": retained})
            context.update(entities=packet["entities"], model_entity_ids=[e["id"] for e in retained], deferred_entities=deferred)
            deferred_all.extend({"packet_id": rid, **entry} for entry in deferred)
        rows.append(runner_row(rid, prompt))
        contexts.append(context)
    definition = {"version": "lean-entity-only-v1", "system_prompt": ENTITY_PROMPT} if stage == "entities" else {
        "version": "summary-ablation-v1", "system_prompt": remove_statement_prompt(RELATION_PROMPT),
    }
    # Validate every packet before writing anything. Frozen files cannot be overwritten.
    output.mkdir(parents=True, exist_ok=True)
    freeze(output / "inputs.json", {"records": rows})
    freeze(output / "definition.json", definition)
    freeze(output / "source-context.json", {"version": "original-context-v1", "generated": False,
        "note": "Full original article/passages retained; context is not an automatically verified claim. Original inventory includes deferred entities excluded from model input.",
        "records": contexts})
    freeze(output / "plan.json", {
        "version": VERSION, "stage": stage, "sample_count": len(rows), "engine_state": "not_run",
        "summary_generation": False, "statement_generation": False, "production_write": False,
        "integration": "Research input preparation only. Existing entity assembly and relation validation remain separate; production Evidence.assertion is unchanged.",
        "workflow": "Source-only model input, original provenance retained, no model call or semantic correction during preparation.",
        "deferred_entities": deferred_all,
        "deferral_rule": "Keep unlocated extras in original context, exclude them from relation hints; no invented location or entity. This implements existing 013 optional-entity handling, not a new measured model benefit.",
        "source": {"path": str(packets_path), "sha256": sha(packets_path)},
        "preparation_code_sha256": sha(Path(__file__)),
    })
    return {"stage": stage, "records": len(rows), "deferred_entities": len(deferred_all), "output": str(output), "inference": "not_run"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("entities", "relations"))
    parser.add_argument("--packets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.stage, args.packets, args.output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Preparation rejected: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
