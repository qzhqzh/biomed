"""Freeze three post-hoc source-context counterexamples for a bounded probe."""

import json
from pathlib import Path

from scripts.freeze_effective_knowledge_sample import materialize
from scripts.knowledge_observation_report import DEFAULT_ROOT, digest

CASES = (
    ("kp-20261107T044401-44c25d51", "42840361", "E-42840361-0010", "review_cited_trials"),
    ("kp-20261107T133251-da6119a7", "42864811", "E-42864811-0036", "preclinical_pdx"),
    ("kp-20261108T045422-4d945504", "42817873", "E-42817873-0046", "treatment_receipt_and_multiple_cohorts"),
)


def prepare(output):
    selected = [{"batch_id": batch, "pmid": pmid, "group_id": group,
                 "sample_role": "post_hoc_context_probe", "status": "diagnostic_only", "reason_code": reason}
                for batch, pmid, group, reason in CASES]
    samples = materialize(DEFAULT_ROOT, selected)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in (("samples.json", samples), ("manifest.json", {
        "version": "target-context-probe-v1", "samples_sha256": digest(samples),
        "selection": "Post-hoc selected difficult examples, not an independent accuracy sample",
        "source_selection": "First 80 completed/partial queue rows of priority >=3; three distinct PMIDs with an old treatment fact and gene/variant/drug sharing an evidence paragraph; one development PMID excluded.",
        "target_contacted": False, "target_written": False,
    })):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    return {"output": str(output), "samples": len(samples)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(json.dumps(prepare(parser.parse_args().output), ensure_ascii=False))
