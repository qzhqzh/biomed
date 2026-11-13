"""Prepare one bounded source-only relation refinement; never write production data."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_knowledge_candidates import freeze, read, sha
from scripts.summary_ablation_pilot import _copy_frozen
from scripts.summary_assist_pilot import EXPECTED, token_preflight, validate_inputs

VERSION = "source-relation-refinement-v1"
DEFAULT_ROOT = Path("var/experiments/source-refinement-20261030")
BASE = Path("var/experiments/summary-ablation-20261029")
ENTITY_BASE = Path("var/experiments/lean-entities-20261029")
CONSTRAINTS = """Source fidelity for the selected ANCHOR fact:
- scope describes the experiment or population belonging to the selected ANCHOR finding. SUPPORT may supply necessary disease, regimen or cohort context, but do not import a different experiment or the whole article's combined study scope into this fact.
- Keep each follow-up observation attached to its own timepoint in results.group and results.value. Preserve original relative-time wording with its source context; do not calculate a new date, rewrite its reference point, or merge different visits and outcomes into one timing claim.
- In results.value retain the source's negation, lack-of-evidence, uncertainty and recommendation qualifiers. Prefer the original qualifying clause over a stronger paraphrase: absence of demonstrated benefit is not proof of no benefit. Do not omit a qualification essential to the claim you select.
These are generation constraints, not a request for new fields, a summary or a second review. Keep the existing output schema and specific original drug names."""


def refined_prompt(prompt):
    if "- statement:" in prompt or CONSTRAINTS in prompt:
        raise ValueError("Expected the frozen source-only baseline prompt")
    return prompt + "\n\n" + CONSTRAINTS


def prepare(root, reference, audit):
    root, reference, audit = Path(root), Path(reference), Path(audit)
    inputs, packets = read(BASE / "inputs.json"), read(BASE / "packets.json")
    ids = validate_inputs(inputs)
    if ids != [p["id"] for p in packets["records"]]:
        raise ValueError("The same ordered 15 source groups are required")
    config = read(BASE / "extract/manifest.json")["config"]
    if any(config.get(k) != v for k, v in EXPECTED.items()):
        raise ValueError("Historical generation configuration changed")
    read(reference)
    read(audit)
    sources = [BASE / name for name in (
        "inputs.json", "packets.json", "definition.json", "reference.json", "review.json",
        "extract/manifest.json", *(f"extract/result-{rid}.json" for rid in ids),
    )]
    sources += [ENTITY_BASE / name for name in ("assembled.json", "source-inputs.json", "guard-metadata.json")]
    root.mkdir(parents=True, exist_ok=True)
    for name in ("inputs.json", "packets.json"):
        _copy_frozen(BASE / name, root / name)
    _copy_frozen(BASE / "definition.json", root / "baseline-definition.json")
    _copy_frozen(reference, root / "reference.json")
    _copy_frozen(audit, root / "failure-audit.json")
    prompt = refined_prompt(read(BASE / "definition.json")["system_prompt"])
    freeze(root / "definition.json", {"version": VERSION, "system_prompt": prompt})
    freeze(root / "plan.json", {
        "version": VERSION, "sample_count": 15, "document_count": 5, "group_ids": ids,
        "baseline": {"entities": str(ENTITY_BASE), "relations": str(BASE)},
        "design": "Entity rules replay the original 010 outputs with explicitly cited source definitions. Independently, compare 15 original 014 relation outputs with 15 new calls; user input, original passages, existing entity inventory and output schema are unchanged.",
        "new_scored_calls": 15, "extra_model_review_calls": 0, "summary_calls": 0,
        "prompt_change": CONSTRAINTS,
        "generation": {**EXPECTED, "model_path": "/data/models/Qwen3.5-9B", "revision": config["revision"], "gpu_uuid": config["gpu_uuid"]},
        "adoption": "Evaluate entity rules and relation prompt independently. Require source support and no new entity errors for each rule. Adopt the relation prompt only if it repairs prior failures without losing any previously passing group or adding core misbinding; report measured cost. No iterative prompt tuning in this study.",
        "limits": ["Development regression, not unseen-paper validation.", "No new entity-to-relation end-to-end run.", "Same Astra semantic criteria; verbatim phrasing is a generation preference, not a new pass criterion.", "Keep raw originals, failures and denominator; no summary, training, repair pass or production writes."],
        "reference_sha256": sha(root / "reference.json"), "audit_sha256": sha(root / "failure-audit.json"),
        "sources": [{"path": str(p), "sha256": sha(p)} for p in sources],
    })
    return {"root": str(root), "records": 15, "new_calls": 15, "summary_calls": 0}


def preflight(root):
    for key, value in {"HF_HOME": "/data/models/.cache/huggingface", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "USE_TF": "0", "USE_FLAX": "0"}.items():
        os.environ[key] = value
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained("/data/models/Qwen3.5-9B", local_files_only=True, trust_remote_code=False)
    result = token_preflight(read(root / "inputs.json"), read(root / "definition.json"), tokenizer)
    freeze(root / "preflight.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--preflight", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args()
    if args.prepare and (not args.reference or not args.audit):
        parser.error("--prepare requires --reference and --audit")
    result = prepare(args.root, args.reference, args.audit) if args.prepare else preflight(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.preflight and not result["ready"]:
        sys.exit(1)
