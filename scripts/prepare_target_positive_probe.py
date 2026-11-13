"""Freeze a post-hoc clinical positive that the old adapter did not map."""

import json
from pathlib import Path

from scripts.freeze_effective_knowledge_sample import materialize
from scripts.knowledge_observation_report import DEFAULT_ROOT, digest


def prepare(output):
    selected = [{"batch_id": "kp-20261109T192719-36f76f51", "pmid": "42719457",
                 "group_id": "E-42719457-0015", "sample_role": "post_hoc_positive_probe",
                 "status": "diagnostic_only", "reason_code": "clinical_egfr_treatment_result"}]
    samples = materialize(DEFAULT_ROOT, selected)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"version": "target-positive-probe-v1", "samples_sha256": digest(samples),
                "selection": "Post-hoc clinical positive found in 217 existing processed papers; not an independent accuracy sample",
                "target_contacted": False, "target_written": False}
    for name, data in (("samples.json", samples), ("manifest.json", manifest)):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    return {"output": str(output), "samples": len(samples)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(json.dumps(prepare(parser.parse_args().output), ensure_ascii=False))
