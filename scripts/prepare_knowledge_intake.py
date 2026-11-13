"""Prepare a source-preserving candidate import from frozen research results. No model calls."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

RUN = "knowledge-intake-20261030"
VERSION = "knowledge-intake-v1"


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text() != payload:
            raise ValueError(f"Refusing to overwrite {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(payload)


def whole_mention(text, start, end):
    """Do not accept ER inside HER2; preserve punctuation and original case."""
    word = lambda c: c.isalnum() or c == "_"
    # An extracted singular remains an exact source span in an English plural.
    # This never rewrites the quote or expands its offsets (e.g. CRP responders).
    plural_suffix = text[end:end + 1] == "s" and (
        end + 1 == len(text) or not word(text[end + 1])
    )
    return not (
        (start > 0 and word(text[start]) and word(text[start - 1]))
        or (end < len(text) and word(text[end - 1]) and word(text[end]) and not plural_suffix)
    )


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"entity_ids", "evidence", "passage"}:
                yield from strings(item)


def association(entity, fact):
    if entity["id"] in fact.get("entity_ids", []):
        return "model_selected"
    for value in strings(fact):
        for match in re.finditer(re.escape(entity["text"]), value):
            if whole_mention(value, *match.span()):
                return "exact_fact_text"
    return "context_only"


def subtype(entity):
    label, kind = entity["text"], entity["type"]
    if kind == "treatment":
        if entity.get("type_refinement", {}).get("original_type") == "drug":
            return "category"
        if "+" in label:
            return "regimen"
    if kind == "biomarker":
        if "kinetics" in label.lower():
            return "dynamic_feature"
        if "signature" in label.lower():
            return "signature"
    return ""


def build(experiments):
    base = experiments / "summary-ablation-20261029"
    types_path = experiments / "source-refinement-20261030/entity-types.json"
    packets = read(base / "packets.json")["records"]
    typed = {r["id"]: r for r in read(types_path)["records"]}
    review_file = read(base / "review.json")
    reviews = {r["id"]: r for r in review_file["records"]}
    if set(typed) != set(reviews) or set(typed) != {p["id"] for p in packets} or len(packets) != 15:
        raise ValueError("Expected the same 15 groups")
    sources = {
        "016_entities": sha(types_path), "014_packets": sha(base / "packets.json"),
        "014_review": sha(base / "review.json"), "014_reference": sha(base / "reference.json"),
    }
    records, exclusions, unresolved = [], [], []
    kinds, associations = Counter(), Counter()
    for packet in packets:
        group = packet["id"]
        result_path = base / "extract" / f"result-{group}.json"
        result = read(result_path)
        fact = result["payload"]["fact"]
        review = reviews[group]
        if review["result_sha256"] != sha(result_path):
            raise ValueError(f"{group}: review does not refer to this result")
        if typed[group]["article"] != packet["article"]:
            raise ValueError(f"{group}: article changed")
        passages = {p["id"]: p for p in packet["passages"]}
        entities, anchors, missing, excluded = [], [], [], []
        for entity in typed[group]["entities"]:
            kind, name = entity["type"], entity["text"]
            kinds[kind] += 1
            linked = association(entity, fact)
            associations[f"{kind}:{linked}"] += 1
            original_type = entity.get("type_refinement", {}).get("original_type", kind)
            roles = ["biomarker_candidate"] if original_type == "biomarker" else []
            valid = []
            for loc in entity["locations"]:
                passage = passages[loc["passage_id"]]
                start, end = loc["start"], loc["end"]
                if not 0 <= start < end <= len(passage["text"]) or passage["text"][start:end] != name:
                    raise ValueError(f"{group}/{entity['id']}: original quote mismatch")
                if not whole_mention(passage["text"], start, end):
                    excluded.append({"entity_ref": entity["id"], "quote": name, **loc,
                                     "reason": "embedded_in_larger_token"})
                    continue
                valid.append(loc)
                anchors.append({
                    "entity_ref": entity["id"], "passage_id": passage["id"],
                    "parser_version": passage["parser_version"], "locator": passage["locator"],
                    "start": start, "end": end, "quote": name, "purpose": "mention",
                    "association": linked, "roles": roles,
                    "attributes": {"original_type": original_type,
                                   "standard_link": entity.get("link", {}),
                                   "role_basis": "extraction_type_unverified" if roles else "none",
                                   "input_role": loc["role"], "parser_version": passage["parser_version"]},
                })
            if not valid:
                missing.append({"entity_ref": entity["id"], "label": name, "kind": kind,
                                "reason": "no_valid_source_location", "original_status": entity["status"]})
                continue
            entities.append({"ref": entity["id"], "kind": kind, "subtype": subtype(entity),
                             "label": name, "attributes": {"identity_status": "source_scoped_candidate"}})
        # All original input paragraphs are retained, separately labelled from cited evidence.
        for passage in packet["passages"]:
            cited = passage["alias"] in fact.get("evidence", [])
            anchors.append({
                "entity_ref": None, "passage_id": passage["id"], "start": 0,
                "parser_version": passage["parser_version"], "locator": passage["locator"],
                "end": len(passage["text"]), "quote": passage["text"],
                "purpose": "evidence" if cited else "context", "association": "context_only",
                "roles": [], "attributes": {"alias": passage["alias"], "input_role": passage["role"],
                                             "model_cited": cited, "parser_version": passage["parser_version"]},
            })
        # A local definition is its own citation, never a fabricated treatment effect.
        for change in typed[group]["changes"]:
            for evidence in change["evidence"]:
                if evidence["kind"] != "explicit_acronym_definition":
                    continue
                if evidence["version_id"] != packet["article"]["version_id"]:
                    raise ValueError("Definition belongs to a different version")
                anchors.append({"entity_ref": change["entity_id"],
                                "passage_id": evidence["passage_id"], "start": evidence["start"],
                                "parser_version": evidence["parser_version"], "locator": evidence["locator"],
                                "end": evidence["end"], "quote": evidence["quote"],
                                "purpose": "definition", "association": "definition", "roles": [],
                                "attributes": {"rule_id": change["rule_id"],
                                               "parser_version": evidence["parser_version"]}})
        records.append({
            "group_id": group, "version_id": packet["article"]["version_id"],
            "source_sha256": packet["article"]["source_sha256"], "article": packet["article"],
            "fact": fact, "assessment": {**review, "reference_model": review_file["model"],
                                         "status": "model_reference_not_human_gold"},
            "provenance": {"relation_result_sha256": sha(result_path),
                           "relation_input_sha256": result["input_sha256"],
                           "relation_config_sha256": result["config_sha256"],
                           "unresolved_entities": missing, "excluded_locations": excluded,
                           "type_changes": typed[group]["changes"]},
            "entities": entities, "anchors": anchors,
        })
        exclusions.extend({"group_id": group, **item} for item in excluded)
        unresolved.extend({"group_id": group, **item} for item in missing)
    bundle = {"schema_version": "knowledge-candidates-v1", "source_run": RUN,
              "provenance": {"version": VERSION, "sources": sources, "new_model_calls": 0,
                             "relation_baseline": "014-source-only", "entity_types": "016",
                             "association_policy": "selected ID, else case-sensitive whole original term in fact, else context"},
              "records": records}
    report = {"version": VERSION, "source_run": RUN, "groups": len(records),
              "articles": len({r["version_id"] for r in records}), "input_mentions_by_kind": dict(kinds),
              "association_counts": dict(associations),
              "stored_group_entities": sum(len(r["entities"]) for r in records),
              "anchors": sum(len(r["anchors"]) for r in records),
              "retained_fact_reference_passes": sum(r["assessment"]["structured_common_core_usable"] for r in records),
              "unresolved": unresolved, "excluded_locations": exclusions, "new_model_calls": 0,
              "sources": sources}
    return bundle, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiments", type=Path, default=PROJECT / "var/experiments")
    parser.add_argument("--output", type=Path, default=PROJECT / "var/experiments" / RUN)
    args = parser.parse_args()
    bundle, report = build(args.experiments)
    freeze(args.output / "bundle.json", bundle)
    freeze(args.output / "preparation.json", report)
    print(json.dumps(report, ensure_ascii=False))
