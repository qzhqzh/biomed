"""Exercise only two pure functions read from the unchanged target source."""

import ast
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
TARGET = Path("/home/zhuqin/project/zhenyuan/tumor_api/src/biomeddb")
BASELINE = PROJECT / "var/experiments/target-yield-20261106/target-contract-probes.json"
OUTPUT = Path("/appdata/02_resource/apps/biomed/research/artifacts/target-identity-scope-20261112-v1/target-matcher-probes-v1.json")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    matcher = TARGET / "services/logical_target.py"
    serializer = TARGET / "serializers/evidence_center.py"
    if sha(matcher) != baseline["matcher_sha256"] or sha(serializer) != baseline["serializer_sha256"]:
        raise ValueError("Target source changed since the frozen contract; do not claim compatibility")
    tree = ast.parse(matcher.read_text(encoding="utf-8"), filename=str(matcher))
    names = {"normalize_gene_symbol", "normalize_target_key"}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != names:
        raise ValueError("Expected pure target normalization functions")
    # These two audited functions use only `re` on the mutation branch. No
    # target package imports, database initialization, writes, or target calls.
    namespace = {"re": re, "LogicalTargetEventType": SimpleNamespace(MUTATION="mutation", CNV="cnv", FUSION="fusion")}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(matcher), "exec"), namespace)  # noqa: S102 - fixed-source, hash-checked pure functions only

    def normalized(gene, value):
        return (namespace["normalize_gene_symbol"](gene), namespace["normalize_target_key"]("mutation", value))

    probes = []
    for gene, variant, wrong_variant, wrong_gene in (("EGFR", "L861Q", "L858R", "KRAS"),
                                                     ("KRAS", "G12C", "G12V", "EGFR")):
        expected = normalized(gene, variant)
        positive = normalized(gene, "p." + variant) == expected
        wrong_position = normalized(gene, wrong_variant) == expected
        different_gene = normalized(wrong_gene, variant) == expected
        if not positive or wrong_position or different_gene:
            raise ValueError("Target mutation normalization failed a synthetic probe")
        probes.append({"gene": gene, "variant": variant, "p_prefix_matches": positive,
                       "different_variant_matches": wrong_position, "different_gene_matches": different_gene})
    return {"version": "biomed.readonly-target-matcher-probe.v1", "serializer_sha256": sha(serializer),
            "matcher_sha256": sha(matcher), "probes": probes, "target_database_connected": False,
            "target_project_modified": False, "live_serializer_exercised": False,
            "note": "Synthetic normalization only; no patient matching, production entities or clinical-condition enforcement."}


if __name__ == "__main__":
    result = probe()
    with OUTPUT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"probes": len(result["probes"]), "target_project_modified": False}, ensure_ascii=False))
