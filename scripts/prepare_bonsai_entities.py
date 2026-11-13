"""Freeze 010 entity inputs and current 016 scoring; no model or database calls."""
# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.evaluate_knowledge_candidates import freeze, read, sha

BASE = PROJECT / "var/experiments/lean-entities-20261029"
GATE = PROJECT / "var/experiments/summary-assist-20261029"
RULES = PROJECT / "var/experiments/source-refinement-20261030"
CONTEXT = PROJECT / "var/experiments/evidence-passages-20261023/inputs.json"
ROOT = PROJECT / "var/experiments/bonsai-stages-20261101/entities"
IDS = [f"K{i:02d}" for i in range(1, 16)]


def copy_frozen(source, target):
    content = source.read_bytes()
    if target.exists() and target.read_bytes() != content:
        raise ValueError(f"Frozen destination differs: {target}")
    if not target.exists():
        target.write_bytes(content)


def prepare(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    sources = []
    for name in ("inputs.json", "definition.json", "source-inputs.json", "source-context.json", "guard-metadata.json"):
        source = BASE / name
        copy_frozen(source, root / name)
        sources.append({"path": str(source), "sha256": sha(source), "copy": name})
    for source, name in ((BASE / "reference.json", "legacy-reference.json"),
                         (CONTEXT, "fulltext-context.json"),
                         (RULES / "reference.json", "historical-016-reference.json"),
                         (RULES / "review.json", "historical-016-review.json")):
        copy_frozen(source, root / name)
        sources.append({"path": str(source), "sha256": sha(source), "copy": name})
    current, old_review = read(RULES / "reference.json"), read(RULES / "review.json")
    post = {r["id"]: r["entities"] for r in old_review["records"]}
    scoring = read(GATE / "reference.json")["entity_scoring"]
    reference = {
        "version": "bonsai-entities-reference-v1", "model": "gpt-6-astra",
        "status": "frozen", "origin": "Reuse existing 015/016 core requirements and 016 rules, not new reference labels",
        "entity_scoring": scoring,
        "records": [{"id": r["id"], "entities": r["entities"],
                     "baseline_after_source_rules": post[r["id"]]} for r in current["records"]],
        "limits": ["AI reference, not human gold", "Standard-link accuracy not measured", "Core completion is not all-entity precision or relation participation"],
    }
    freeze(root / "reference.json", reference)
    if [r["id"] for r in read(root / "inputs.json")["records"]] != IDS:
        raise ValueError("Expected the ordered frozen 15 entity inputs")
    sources += [{"path": str(p), "sha256": sha(p)} for p in
                (GATE / "reference.json", BASE / "assembled.json", BASE / "extract/manifest.json", BASE / "extract/summary.json")]
    sources += [{"path": str(BASE / "extract" / f"result-{rid}.json"),
                 "sha256": sha(BASE / "extract" / f"result-{rid}.json")} for rid in IDS]
    freeze(root / "plan.json", {
        "version": "bonsai-entities-plan-v1", "stage": "entities", "sample_count": 15,
        "baseline": str(BASE), "reference_sha256": sha(root / "reference.json"),
        "new_bonsai_calls": 15, "new_qwen_calls": 0, "training": False,
        "max_input_tokens": 4096, "max_new_tokens": 2048,
        "sources": sources,
        "comparison": "Identical frozen 010 inputs and prompt; old 9B reviews reused under current 016 gate. Score raw, common class projection, and common source-type rules separately.",
        "link_scope": "Existing frozen standard dictionaries used only to reproduce the same guards/projection. No model disambiguation; link accuracy not scored.",
    })
    print(json.dumps({"stage": "entities", "state": "prepared", "root": str(root), "records": 15}))


def assemble(root):
    from apps.curation.core_knowledge_projection import project
    from scripts.lean_entity_pilot import assemble as assemble_entities
    from scripts.refine_source_entity_types import build as refine_entities

    root = Path(root)
    if not (root / "assembled.json").exists():
        assemble_entities(root)
    if not (root / "entity-types.json").exists():
        freeze(root / "entity-types.json", refine_entities(
            root / "source-inputs.json", root / "assembled.json", root / "fulltext-context.json"))
    packets = {p["id"]: p for p in read(root / "source-inputs.json")["records"]}
    records = []
    for row in read(root / "assembled.json")["records"]:
        packet = {**packets[row["id"]], "entities": row["entities"]}
        projections = [{"source_name": e["text"], "projection": project({"fact": {"drug_names": [e["text"]]}}, packet)}
                       for e in row["entities"] if "text" in e]
        records.append({"id": row["id"], "projections": projections})
    freeze(root / "common-projections.json", {"records": records, "scope": "Diagnostic typed dispatch only; not proof an entity participates in a relation"})
    print(json.dumps({"stage": "entities", "state": "assembled", "root": str(root), "records": len(records)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--assemble", action="store_true")
    args = parser.parse_args()
    (assemble if args.assemble else prepare)(args.root)
