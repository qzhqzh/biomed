"""Fetch/cache PubTator only for an existing selected knowledge-extraction batch.

This is an independent reference artifact. It never edits the 9B inventory,
Bonsai input/output, local knowledge, or tumor_api. Failed remote requests are
recorded and may be retried in a new reference output directory after cooldown.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.curation.pubtator import (
    CACHE_ROOT,
    VERSION,
    Cache,
    atomic_json,
    build_reference,
    read,
    sha,
)
from scripts.light_entity_pilot import STANDARDS


def run_reference(batch, *, output=None, cache_root=CACHE_ROOT, standards=STANDARDS, cache=None):
    batch = Path(batch).resolve()
    output = Path(output).resolve() if output is not None else batch / "pubtator"
    source_path = batch / "stages/entities/assembled-packets.json"
    selection_path = batch / "selected-batch/packets.json"
    standards = Path(standards).resolve()
    inputs = {str(p): sha(p.read_bytes()) for p in (source_path, selection_path, standards)}
    report_path = output / "reference.json"
    if report_path.exists():
        report = read(report_path)
        if report.get("version") != VERSION or report.get("inputs") != inputs:
            raise ValueError("Reference inputs changed; use a new output directory")
        return report["summary"]
    source, selection = read(source_path), read(selection_path)
    selected = {r["id"]: r for r in selection["records"]}
    ids = [r["id"] for r in source["records"]]
    if len(ids) != len(set(ids)) or set(ids) != set(selected):
        raise ValueError("Reference scope must equal the selected extraction packets")
    for packet in source["records"]:
        original = selected[packet["id"]]
        if any(packet[field] != original[field] for field in ("article", "passages")):
            raise ValueError("Reference source differs from selected article version/text")
    client = cache or Cache(cache_root)
    try:
        report = build_reference(source, client, read(standards))
        report["summary"]["state"] = "partial" if report["summary"]["unavailable_views"] else "completed"
    except Exception as exc:  # noqa: BLE001 -- isolate an optional provider from completed local extraction
        # An optional provider/cache/dictionary failure cannot replace or undo
        # completed local extraction. Preserve a visible, bounded error report.
        report = {"summary": {"version": VERSION, "mode": "dual_reference", "state": "unavailable",
                              "local_models_required": ["9B", "Bonsai"], "local_models_skipped": False,
                              "local_result_overwritten": False, "candidates": 0,
                              "requests": client.requests, "response_bytes": client.response_bytes,
                              "error": f"{type(exc).__name__}: {str(exc)[:500]}", "target_written": False},
                  "articles": {}, "records": []}
    report.update(version=VERSION, inputs=inputs)
    report["summary"].update(reference_file=str(report_path), cache_root=str(Path(cache_root).resolve()))
    atomic_json(report_path, report)
    return report["summary"]


def optional_reference(batch, *, cache_root=CACHE_ROOT):
    """Keep the primary pipeline usable even if the reference storage is offline."""
    try:
        return run_reference(batch, cache_root=cache_root)
    except (OSError, ValueError, KeyError) as exc:
        return {"version": VERSION, "mode": "dual_reference", "state": "unavailable",
                "local_models_skipped": False, "local_result_overwritten": False,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Separate reference version; default BATCH/pubtator")
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT)
    parser.add_argument("--standards", type=Path, default=STANDARDS)
    args = parser.parse_args()
    print(json.dumps(run_reference(args.batch, output=args.output, cache_root=args.cache_root,
                                   standards=args.standards), ensure_ascii=False))


if __name__ == "__main__":
    main()
