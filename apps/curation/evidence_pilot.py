"""Read-only source export and resumable offline experiments; never publish evidence."""

import csv
import fcntl
import hashlib
import heapq
import io
import json
import re
import sqlite3
import subprocess
import tarfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apps.curation.decision import checked_input, digest, validate_decision
from apps.curation.pilot_candidates import VERSION as CANDIDATE_VERSION
from apps.curation.pilot_candidates import locate_candidates

VERSION = "evidence-pilot-v1"
EXPOSURE_FILES = (
    "evaluations/solid-tumor-eval-v1/split-manifest.json",
    "evaluations/solid-tumor-eval-v1/development-reviewed.json",
    "evaluations/solid-tumor-eval-v1/holdout-reviewed.json",
    "evaluations/solid-tumor-eval-v1/publication-status-audit.json",
    "decisions/qwen3-1.7b-pilot-v1/selected-inputs.json",
    "decisions/qwen3-1.7b-pilot-v1/run/inputs.json",
    "decisions/qwen3-1.7b-pilot-v2/run/inputs.json",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path, content):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def locked(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "pilot.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield folder


def read_query(connection, sql, params=()):
    """Wait for the collector instead of taking a write lock or changing its settings."""
    for attempt in range(6):
        deadline = time.monotonic() + 4
        connection.set_progress_handler(
            lambda deadline=deadline: int(time.monotonic() > deadline), 1000
        )
        try:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            if not any(word in str(exc).lower() for word in ("locked", "busy")):
                raise
            if attempt == 5:
                raise
            time.sleep(min(0.1 * 2**attempt, 2))
        finally:
            connection.set_progress_handler(None, 0)


def read_source(database, limit, selection_from=None, selection_file=None):
    """Use bounded rowid scans: the live table has no created_at index.

    New appends after cutoff are excluded. Selection columns are insert-time
    values. Each chunk releases its read lock; selected versions/passages are
    subsequently captured in one short ordinary read transaction.
    """
    if not 1 <= limit <= 100:
        raise ValueError("pilot limit must be 1..100")
    if selection_from is not None and selection_file is not None:
        raise ValueError("Choose one selection source")
    connection = sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        cutoff = (
            read_query(connection, "SELECT max(rowid) AS n FROM curation_material")[0][
                "n"
            ]
            or 0
        )
        selected, last, scanned = [], 0, 0
        prior_selection = None
        targeted = None
        if selection_file is not None:
            targeted = json.loads(Path(selection_file).read_text())
            selected = [
                (d["created_at"], d["material_id"]) for d in targeted["records"]
            ]
            if (
                len(selected) != limit
                or len({key for _, key in selected}) != limit
                or not targeted.get("selection_method")
            ):
                raise ValueError("Invalid targeted selection")
            last = cutoff
        if selection_from is not None:
            previous = load_snapshot(selection_from)["snapshot"]
            if (
                previous["database"] != str(Path(database).resolve())
                or previous["limit"] != limit
            ):
                raise ValueError("Selection source/database/limit mismatch")
            selected = [
                (d["created_at"], d["material_id"]) for d in previous["documents"]
            ]
            if len(selected) != limit or len(set(selected)) != limit:
                raise ValueError("Invalid frozen selection")
            prior_selection = previous["selection"]
            last = cutoff
        while last < cutoff:
            rows = read_query(
                connection,
                "SELECT rowid AS sequence,id,created_at,kind FROM curation_material "
                "WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 256",
                (last, cutoff),
            )
            if not rows:
                break
            selected = heapq.nsmallest(
                limit,
                selected
                + [
                    (row["created_at"], row["id"])
                    for row in rows
                    if row["kind"] == "paper"
                ],
            )
            last, scanned = rows[-1]["sequence"], scanned + len(rows)
            time.sleep(0.005)
        if len(selected) != limit:
            raise ValueError(f"Only {len(selected)} papers exist; requested {limit}")
        bundles = []
        connection.execute("BEGIN")
        for created, material_id in selected:
            material = read_query(
                connection,
                "SELECT id,source_key,kind,title,identifiers,created_at FROM curation_material WHERE id=?",
                (material_id,),
            )[0]
            if material["created_at"] != created or material["kind"] != "paper":
                raise ValueError(
                    "Selection changed during export; retry into a fresh snapshot"
                )
            material["identifiers"] = json.loads(material["identifiers"])
            versions = read_query(
                connection,
                "SELECT * FROM curation_materialversion WHERE material_id=? ORDER BY acquired_at DESC,id DESC",
                (material_id,),
            )
            passages, classifications = [], []
            for version in versions:
                if version["media_type"] in ("application/pdf", "application/jats+xml"):
                    passages.extend(
                        read_query(
                            connection,
                            "SELECT * FROM curation_passage WHERE version_id=? ORDER BY parser_version,ordinal,id",
                            (version["id"],),
                        )
                    )
                if version["media_type"] == "application/pubmed+xml":
                    classifications.extend(
                        read_query(
                            connection,
                            "SELECT taxonomy_version,result FROM curation_literatureclassification WHERE version_id=?",
                            (version["id"],),
                        )
                    )
            for row in classifications:
                row["result"] = json.loads(row["result"])
            bundles.append(
                {
                    "material": material,
                    "versions": versions,
                    "passages": passages,
                    "stored_classifications": classifications,
                }
            )
        connection.commit()
        return bundles, {
            "cutoff_rowid": cutoff,
            "scanned": scanned,
            "order": (
                "supplied records order"
                if targeted is not None
                else prior_selection["order"]
                if prior_selection is not None
                else "created_at ASC,id ASC"
            ),
            "captured_at": now(),
            "prior_selection": prior_selection,
            "targeted_selection": targeted,
        }
    finally:
        connection.close()


def verified_blob(library, version):
    sha = version["sha256"]
    if version["integrity_status"] != "valid" or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("Source version integrity is not valid")
    content = (Path(library) / "blobs" / sha[:2] / sha).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("Source blob hash mismatch")
    return content


def copy_sources_via_worker(bundles, output):
    """Read as the existing service UID; never chmod production files or write its DB."""
    digests = sorted(
        {
            v["sha256"]
            for bundle in bundles
            for v in bundle["versions"]
            if v["integrity_status"] == "valid"
        }
    )
    if len(digests) > 400 or any(
        not re.fullmatch(r"[a-f0-9]{64}", sha) for sha in digests
    ):
        raise ValueError("Invalid or excessive source selection")
    reader = """import io,json,re,sys,tarfile
from apps.curation.storage import read_blob
digests=json.load(sys.stdin)
assert isinstance(digests,list) and len(digests)<=400
total=0
with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
 for sha in digests:
  assert re.fullmatch(r"[a-f0-9]{64}",sha)
  content=read_blob(sha)
  total+=len(content)
  if total>67108864: raise ValueError("Pilot source copy exceeds 64 MiB budget")
  info=tarfile.TarInfo("blobs/"+sha[:2]+"/"+sha)
  info.size=len(content)
  info.mode=0o640
  archive.addfile(info,io.BytesIO(content))
"""
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "DJANGO_SETTINGS_MODULE=config.settings",
            "worker",
            "python",
            "-c",
            reader,
        ],
        cwd=Path(__file__).resolve().parents[2],
        input=json.dumps(digests).encode(),
        capture_output=True,
        timeout=120,
        check=True,
    )
    root = Path(output) / "source-library"
    expected, seen = set(digests), set()
    with tarfile.open(fileobj=io.BytesIO(completed.stdout)) as archive:
        for member in archive:
            sha = Path(member.name).name
            if (
                sha not in expected
                or sha in seen
                or member.name != f"blobs/{sha[:2]}/{sha}"
                or not member.isfile()
            ):
                raise ValueError("Unexpected source archive entry")
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != sha:
                raise ValueError("Exported source hash mismatch")
            destination = root / member.name
            if any(
                path.is_symlink()
                for path in (root, root / "blobs", destination.parent, destination)
            ):
                raise ValueError("Source copy destination must not contain symlinks")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != content:
                raise ValueError("Existing source copy differs")
            if not destination.exists():
                destination.write_bytes(content)
            seen.add(sha)
    if seen != expected:
        raise ValueError("Incomplete source archive")
    return root


def exposure_index(library):
    index, checked, missing = {}, [], []

    def visit(value, filename):
        if isinstance(value, dict):
            if value.get("pmid"):
                index.setdefault(str(value["pmid"]), set()).add(filename)
            for child in value.values():
                visit(child, filename)
        elif isinstance(value, list):
            for child in value:
                visit(child, filename)

    for name in EXPOSURE_FILES:
        path = Path(library) / name
        if path.is_file():
            visit(json.loads(path.read_text()), name)
            checked.append(name)
        else:
            missing.append(name)
    return index, {
        "checked": checked,
        "missing": missing,
        "status": "incomplete" if missing else "checked_known_artifacts",
        "unseen_not_proven": True,
    }


def prepare_document(bundle, rank, library, exposures, taxonomy):
    # Helpers are pure here; their modules require Django, configured in-memory by CLI.
    from apps.curation.classification import classify_metadata
    from apps.curation.materials import (
        PARSER_VERSION,
        pubmed_metadata,
        pubmed_record_id,
        pubmed_records,
    )
    from apps.curation.network import xml

    material, versions = bundle["material"], bundle["versions"]
    pmid = str(
        material["identifiers"].get("pubmed", material["identifiers"].get("pmid", ""))
    )
    issues, metadata, model_input, metadata_version = [], None, None, None
    candidates = [
        v
        for v in versions
        if v["media_type"] == "application/pubmed+xml"
        and v["integrity_status"] == "valid"
    ]
    if candidates:
        metadata_version = candidates[0]
        try:
            articles = pubmed_records(xml(verified_blob(library, metadata_version)))
            if (
                len(articles) != 1
                or pubmed_record_id(articles[0]) != pmid
                or articles[0].tag != "PubmedArticle"
            ):
                raise ValueError("PubMed identity or record type mismatch")
            parsed = pubmed_metadata(articles[0])
            metadata = {
                key: parsed[key]
                for key in ("title", "abstract", "mesh", "publication_types")
            }
            model_input = checked_input(
                {
                    "pmid": pmid,
                    "version_id": metadata_version["id"],
                    "source_sha256": metadata_version["sha256"],
                    "input_sha256": digest(metadata),
                    "metadata": metadata,
                }
            )
        except PermissionError:
            raise
        except (OSError, ValueError) as exc:
            issues.append(f"metadata_unusable: {exc}")
            metadata = None
    else:
        issues.append("metadata_source_missing")
    fulltext_version, passages = None, []
    for version in versions:
        if (
            version["media_type"] not in ("application/pdf", "application/jats+xml")
            or version["integrity_status"] != "valid"
        ):
            continue
        group = [
            p
            for p in bundle["passages"]
            if p["version_id"] == version["id"]
            and p["parser_version"] == PARSER_VERSION
        ]
        if not group:
            continue
        try:
            verified_blob(library, version)
        except PermissionError:
            raise
        except (OSError, ValueError) as exc:
            issues.append(f"fulltext_unusable: {version['id']}: {exc}")
            continue
        fulltext_version = version
        passages = [{**p, "kind": "fulltext"} for p in group]
        break
    if not passages:
        issues.append("parsed_fulltext_missing")
        if metadata and metadata["abstract"].strip():
            passages = [
                {
                    "id": f"abstract:{metadata_version['id']}",
                    "version_id": metadata_version["id"],
                    "parser_version": "pubmed-metadata-v3",
                    "ordinal": 0,
                    "locator": "PubMed abstract",
                    "text": metadata["abstract"],
                    "kind": "abstract",
                }
            ]
    exposures = sorted(exposures.get(pmid, set()))
    for row in bundle["stored_classifications"]:
        if row["result"].get("deployment_status") == "experimental" or row[
            "result"
        ].get("method") not in (None, "weak_rules"):
            exposures.append("stored_classification:" + row["taxonomy_version"])
    return {
        "rank": rank,
        "material_id": material["id"],
        "pmid": pmid,
        "title": metadata["title"] if metadata else material["title"],
        "created_at": material["created_at"],
        "source_key": material["source_key"],
        "versions": versions,
        "metadata_version_id": metadata_version["id"] if metadata_version else None,
        "fulltext_version_id": fulltext_version["id"] if fulltext_version else None,
        "metadata": metadata,
        "model_input": model_input,
        "passages": passages,
        "passages_sha256": digest(passages),
        "source_issues": issues,
        "input_scope": "parsed_fulltext"
        if fulltext_version
        else "abstract_only"
        if passages
        else "metadata_only",
        "historical_exposures": exposures,
        "evaluation_role": "development_pipeline",
        "reference_status": "unannotated",
        "independent_holdout": False,
        "rule_baseline": classify_metadata(metadata, taxonomy) if metadata else None,
    }


def load_snapshot(folder):
    envelope = json.loads((Path(folder) / "snapshot.json").read_text())
    snapshot = envelope["snapshot"]
    if envelope["sha256"] != digest(snapshot):
        raise ValueError("Frozen snapshot changed")
    expected = {
        "records": [
            row["model_input"] for row in snapshot["documents"] if row["model_input"]
        ]
    }
    if json.loads((Path(folder) / "model-inputs.json").read_text()) != expected:
        raise ValueError("Model inputs do not match frozen snapshot")
    return envelope


def freeze(
    database,
    library,
    output,
    limit=20,
    selection_from=None,
    exposure_root=None,
    selection_file=None,
    via_worker=False,
):
    from apps.curation.classification import taxonomy_hash, taxonomy_snapshot

    database, library, output = (Path(p).resolve() for p in (database, library, output))
    if selection_from is not None and selection_file is not None:
        raise ValueError("Choose one selection source")
    if output.is_relative_to(library) or output.is_relative_to(database.parent):
        raise ValueError("Experiment outputs must be separate from production data")
    with locked(output):
        if (output / "snapshot.json").exists():
            old = load_snapshot(output)["snapshot"]
            if (
                old["database"] != str(database)
                or old["library"] != str(library)
                or old["limit"] != limit
            ):
                raise ValueError(
                    "Source or limit changed; use a new experiment directory"
                )
            targeted = (
                json.loads(Path(selection_file).read_text())
                if selection_file is not None
                else None
            )
            if targeted != old["selection"].get("targeted_selection"):
                raise ValueError(
                    "Targeted selection changed; use a new experiment directory"
                )
            if selection_from is not None:
                previous = load_snapshot(selection_from)["snapshot"]
                if previous["database"] != str(database) or [
                    (row["created_at"], row["material_id"])
                    for row in previous["documents"]
                ] != [
                    (row["created_at"], row["material_id"]) for row in old["documents"]
                ]:
                    raise ValueError(
                        "Frozen selection changed; use a new experiment directory"
                    )
            return {"state": "reused", "documents": len(old["documents"])}
        bundles, selection = read_source(
            database, limit, selection_from, selection_file
        )
        exposures, exposure_check = exposure_index(exposure_root or library)
        taxonomy = taxonomy_snapshot()
        readable_library = (
            copy_sources_via_worker(bundles, output) if via_worker else library
        )
        documents = [
            prepare_document(bundle, rank, readable_library, exposures, taxonomy)
            for rank, bundle in enumerate(bundles, 1)
        ]
        inputs = [row["model_input"] for row in documents if row["model_input"]]
        if len({row["pmid"] for row in inputs}) != len(inputs):
            raise ValueError(
                "Duplicate PMIDs in first papers; manual identity resolution required"
            )
        snapshot = {
            "version": VERSION,
            "created_at": now(),
            "database": str(database),
            "library": str(library),
            "readable_library": str(readable_library),
            "limit": limit,
            "selection": selection,
            "exposure_check": exposure_check,
            "taxonomy": taxonomy,
            "taxonomy_sha256": taxonomy_hash(taxonomy),
            "documents": documents,
        }
        atomic_json(output / "model-inputs.json", {"records": inputs})
        atomic_json(
            output / "snapshot.json", {"sha256": digest(snapshot), "snapshot": snapshot}
        )
        atomic_json(
            output / "annotations-template.json",
            {
                "snapshot_sha256": digest(snapshot),
                "reference_status": "unannotated",
                "records": [
                    {
                        "rank": d["rank"],
                        "material_id": d["material_id"],
                        "pmid": d["pmid"],
                        "document_labels": None,
                        "evidence_spans": [],
                        "entities": [],
                        "relations": [],
                        "annotator": None,
                        "reviewed_at": None,
                        "notes": "",
                    }
                    for d in documents
                ],
            },
        )
        return {
            "state": "frozen",
            "documents": len(documents),
            "model_inputs": len(inputs),
            "input_scopes": dict(Counter(d["input_scope"] for d in documents)),
            "historically_exposed": sum(
                bool(d["historical_exposures"]) for d in documents
            ),
        }


def run(experiment, max_candidates=100):
    with locked(experiment) as folder:
        frozen = load_snapshot(folder)
        config = {
            "version": VERSION,
            "candidate_version": CANDIDATE_VERSION,
            "max_candidates": max_candidates,
            "snapshot_sha256": frozen["sha256"],
        }
        config_sha = digest(config)
        config_path = folder / "run-config.json"
        if config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError(
                "Run configuration changed; use a new experiment directory"
            )
        atomic_json(config_path, config)
        results = folder / "results"
        results.mkdir(exist_ok=True)
        created = reused = 0
        state = "completed"
        for document in frozen["snapshot"]["documents"]:
            if (folder / "STOP").exists():
                state = "paused"
                break
            path = results / f"case-{document['rank']:02}.json"
            if path.exists():
                previous = json.loads(path.read_text())
                if (
                    previous["config_sha256"] != config_sha
                    or previous["material_id"] != document["material_id"]
                    or previous["result_sha256"] != digest(previous["result"])
                ):
                    raise ValueError("Existing result differs from this experiment")
                reused += 1
                continue
            started = time.monotonic()
            candidate_result = locate_candidates(
                document, max_candidates=max_candidates
            )
            result = {
                "source_state": "available"
                if document["passages"]
                else "waiting_source",
                "rule_classification": document["rule_baseline"],
                "candidate_passages": candidate_result,
                "entity_linking": {"state": "waiting_configuration"},
                "relation_extraction": {"state": "waiting_configuration"},
                "ai_review": {"state": "waiting_configuration"},
                "published_evidence": 0,
                "seconds": round(time.monotonic() - started, 6),
            }
            atomic_json(
                path,
                {
                    "material_id": document["material_id"],
                    "config_sha256": config_sha,
                    "result_sha256": digest(result),
                    "result": result,
                },
            )
            created += 1
            atomic_json(
                folder / "runner-state.json",
                {
                    "state": "running",
                    "updated_at": now(),
                    "processed": created + reused,
                    "total": len(frozen["snapshot"]["documents"]),
                },
            )
        summary = {
            "state": state,
            "updated_at": now(),
            "created": created,
            "reused": reused,
            "processed": created + reused,
            "total": len(frozen["snapshot"]["documents"]),
            "published_evidence": 0,
            "network_requests": 0,
            "training": False,
        }
        atomic_json(folder / "runner-state.json", summary)
        return summary


def model_result(folder, document):
    path = Path(folder) / "model" / f"result-{document['pmid']}.json"
    if not path.exists():
        return {
            "status": "not_run"
            if (Path(folder) / "model" / "manifest.json").exists()
            else "waiting_configuration"
        }
    row = json.loads(path.read_text())
    original = document["model_input"]
    if original is None or any(
        row.get(key) != original[key]
        for key in ("pmid", "version_id", "source_sha256", "input_sha256")
    ):
        raise ValueError("Model result provenance mismatch")
    if row["status"] == "validated_candidate":
        manifest = json.loads((Path(folder) / "model" / "manifest.json").read_text())
        if row.get("config_sha256") != manifest["config_sha256"]:
            raise ValueError("Model result configuration mismatch")
        if validate_decision(row["raw_output"], original["metadata"]) != row.get(
            "decision"
        ):
            raise ValueError("Model decision differs from validated raw output")
    return row


def report(experiment):
    with locked(experiment) as folder:
        frozen = load_snapshot(folder)
        config_path = folder / "run-config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else None
        if config is not None and config.get("snapshot_sha256") != frozen["sha256"]:
            raise ValueError("Run configuration differs from frozen snapshot")
        rows, detail, candidate_total = [], [], 0
        model_statuses = Counter()
        for document in frozen["snapshot"]["documents"]:
            path = folder / "results" / f"case-{document['rank']:02}.json"
            if not path.exists():
                result = {}
            else:
                saved = json.loads(path.read_text())
                if saved["material_id"] != document["material_id"] or saved[
                    "result_sha256"
                ] != digest(saved["result"]):
                    raise ValueError("Pilot result provenance mismatch")
                if config is None or saved["config_sha256"] != digest(config):
                    raise ValueError("Pilot result configuration mismatch")
                result = saved["result"]
            candidate = result.get("candidate_passages", {})
            count = len(candidate.get("candidates", []))
            candidate_total += count
            model = model_result(folder, document)
            model_statuses[model["status"]] += 1
            decision = (
                (model.get("decision") or {})
                if model["status"] == "validated_candidate"
                else {}
            )
            row = {
                "rank": document["rank"],
                "pmid": document["pmid"],
                "title": document["title"],
                "input_scope": document["input_scope"],
                "rule_scope": (document["rule_baseline"] or {}).get(
                    "scope", "waiting_source"
                ),
                "candidate_status": candidate.get("status", "not_run"),
                "candidate_passages": count,
                "model_status": model["status"],
                "model_relevance": decision.get("relevance", ""),
                "model_scope": decision.get("scope", ""),
                "model_seconds": model.get("seconds", ""),
                "model_error": model.get("error", ""),
                "previously_exposed": bool(document["historical_exposures"]),
                "reference_status": document["reference_status"],
            }
            rows.append(row)
            detail.append(
                f"### {document['rank']}. PMID {document['pmid']}\n\n{document['title']}\n\n输入：{document['input_scope']}；规则：{row['rule_scope']}；模型：{row['model_status']} {row['model_relevance']}；候选段落：{count}。\n"
            )
            for item in candidate.get("candidates", [])[:3]:
                detail.append(
                    f"\n位置：{item['locator']}；字符 {item['start']}–{item['end']}。\n\n> "
                    + item["quote"].replace("\n", "\n> ")
                    + "\n"
                )
            if count > 3:
                detail.append(
                    f"\n其余 {count - 3} 段保存在 results/case-{document['rank']:02}.json；展示省略不改变候选产物。\n"
                )
        summary = {
            "snapshot_sha256": frozen["sha256"],
            "documents": len(rows),
            "input_scopes": dict(Counter(row["input_scope"] for row in rows)),
            "rule_scopes": dict(Counter(row["rule_scope"] for row in rows)),
            "candidate_passages": candidate_total,
            "documents_with_candidates": sum(
                row["candidate_passages"] > 0 for row in rows
            ),
            "model_statuses": dict(model_statuses),
            "model_inference_seconds": round(
                sum(row["model_seconds"] or 0 for row in rows), 3
            ),
            "accuracy": None,
            "accuracy_reason": "No independent reference labels; agreement is not accuracy",
            "published_evidence": 0,
            "training": False,
            "source_database_writes": 0,
            "tumor_project_writes": 0,
        }
        atomic_json(folder / "summary.json", summary)
        atomic_json(folder / "comparison.json", {"summary": summary, "records": rows})
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            # Avoid interpreting source-controlled titles as spreadsheet formulas.
            writer.writerow(
                {
                    key: "'" + value
                    if isinstance(value, str)
                    and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                    for key, value in row.items()
                }
            )
        atomic_text(folder / "comparison.csv", stream.getvalue())
        table = [
            "| 序号 | PMID | 输入 | 规则范围 | 候选段落 | 模型状态 | 模型范围 |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
        for row in rows:
            table.append(
                f"| {row['rank']} | {row['pmid']} | {row['input_scope']} | {row['rule_scope']} | {row['candidate_passages']} | {row['model_status']} | {row['model_relevance']} |"
            )
        targeted = frozen["snapshot"]["selection"].get("targeted_selection")
        cohort = f"实体瘤补充 {len(rows)} 篇" if targeted else f"前 {len(rows)} 篇文献"
        selection_note = (
            "本组从已有全文中目的性选择，补充实体瘤癌种与研究类型，不是随机样本，不改变原前 20 篇。"
            if targeted
            else f"前 {len(rows)} 篇保持原选择，未以有全文或模型成功为条件替换。"
        )
        preface = f"# {cohort}离线试验\n\n生成时间：{datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()}（北京时间）。\n\n本报告为开发试验，没有独立金标准；规则标签和段落候选均非已核验知识。正式证据产出为 0，实体规范化、关系抽取与 AI 审核尚待配置。{selection_note}\n\n候选段落 {candidate_total} 个；这些是宽召回结果，不等于正确证据条数。模型处理失败仍计入总数。不计算准确率。\n\n"
        atomic_text(
            folder / "report.md",
            preface + "\n".join(table) + "\n\n## 逐篇原文核对\n\n" + "\n".join(detail),
        )
        return summary
