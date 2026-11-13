"""Inspect exact-name collisions in a frozen, non-patient target dictionary.

Colliding names do not establish that records describe the same entity.  This
report never mutates either dictionary or selects an export ID.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from apps.curation.knowledge_target_mapping import _name_index, _target_name_key


def run(dictionary_path, output, rxnorm_path=None):
    dictionary_path, output = Path(dictionary_path), Path(output)
    if output.exists():
        raise FileExistsError("Preserve the first collision report")
    raw = dictionary_path.read_bytes()
    dictionary = json.loads(raw)
    index = _name_index(dictionary)
    by_kind = {}
    for kind, source in (("drug", "drugs"), ("disease", "tumors")):
        collisions = [{"normalized_name": name, "target_ids": sorted(ids)}
                      for name, ids in sorted(index[kind].items()) if len(ids) > 1]
        unique_ids = {identifier for row in collisions for identifier in row["target_ids"]}
        by_kind[kind] = {"dictionary_entries": len(dictionary[source]),
                         "colliding_names": len(collisions),
                         "entries_involved": len(unique_ids), "collisions": collisions}
    result = {"version": "target-name-collisions-v1", "dictionary_sha256": hashlib.sha256(raw).hexdigest(),
              "scope": "Frozen dictionary exact lookup keys, including aliases; not a duplicate-entity estimate.",
              "by_kind": by_kind, "target_contacted": False, "target_written": False,
              "identity_decisions_made": False}
    if rxnorm_path is not None:
        raw_rxnorm = Path(rxnorm_path).read_bytes()
        rxnorm = defaultdict(set)
        for ingredient in json.loads(raw_rxnorm):
            if ingredient.get("en") and ingredient.get("identifier"):
                rxnorm[_target_name_key(ingredient["en"])].add(ingredient["identifier"])
        english = defaultdict(set)
        for drug in dictionary["drugs"]:
            if drug.get("name_en"):
                english[_target_name_key(drug["name_en"])].add(drug["id"])
        groups = [{"name_en_normalized": name, "target_ids": sorted(ids),
                   "exact_rxnorm_ids": sorted(rxnorm[name])}
                  for name, ids in sorted(english.items()) if len(ids) > 1]
        result["rxnorm_coverage"] = {
            "snapshot_sha256": hashlib.sha256(raw_rxnorm).hexdigest(), "groups": groups,
            "duplicate_english_names": len(groups),
            "unique_exact_ingredient_name": sum(len(group["exact_rxnorm_ids"]) == 1 for group in groups),
            "no_exact_ingredient_name": sum(not group["exact_rxnorm_ids"] for group in groups),
            "multiple_exact_ingredient_names": sum(len(group["exact_rxnorm_ids"]) > 1 for group in groups),
            "scope": "Exact normalized English names only; a name match does not prove both target IDs equivalent.",
        }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    summary = {kind: {key: value for key, value in data.items() if key != "collisions"}
               for kind, data in by_kind.items()}
    if rxnorm_path is not None:
        summary["rxnorm_coverage"] = {key: value for key, value in result["rxnorm_coverage"].items()
                                      if key not in {"groups", "snapshot_sha256", "scope"}}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--rxnorm", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.dictionary, args.output, args.rxnorm), ensure_ascii=False))
