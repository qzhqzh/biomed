"""Bounded, resumable ChEMBL mechanisms and their referenced entities.

No database or application settings are used here. The completed manifest is
the publication boundary; partial files are never a completed source release.
"""

import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://www.ebi.ac.uk/chembl/api/data/"
SCHEMA = "chembl-mechanism-snapshot-v1"
LICENSE = {
    "name": "CC BY-SA 3.0",
    "url": "https://creativecommons.org/licenses/by-sa/3.0/",
    "source_url": "https://www.ebi.ac.uk/chembldb/",
    "attribution": "ChEMBL, EMBL-EBI",
}
INTERVAL = 2
RATE = 256 * 1024
MAX_BYTES = 128 * 1024 * 1024
MAX_REQUESTS = 500
RESPONSE_LIMIT = 8 * 1024 * 1024
PAGE_SIZE = 500
BATCH_SIZE = 50
COLLECTIONS = {
    "mechanism": ("mechanisms", "mec_id"),
    "molecule": ("molecules", "molecule_chembl_id"),
    "target": ("targets", "target_chembl_id"),
}


class SnapshotError(ValueError):
    """Integrity, scope or persistent budget failure; retain all checkpoints."""


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def write_json(path, value):
    data = json_bytes(value)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return {"file": path.name, "sha256": sha256(data), "bytes": len(data)}


def load_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SnapshotError("Duplicate JSON key in source response")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise SnapshotError("Invalid source JSON") from exc


def allowed(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.ebi.ac.uk"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path
        not in {f"/chembl/api/data/{x}.json" for x in (*COLLECTIONS, "status")}
    ):
        raise SnapshotError("Only the scoped official ChEMBL endpoints are allowed")
    endpoint = parsed.path.rsplit("/", 1)[1].split(".")[0]
    params = parse_qs(parsed.query)
    if endpoint in ("molecule", "target"):
        values = params.get(f"{endpoint}_chembl_id__in", [])
        if len(values) != 1 or not 1 <= len(values[0].split(",")) <= BATCH_SIZE:
            raise SnapshotError("Entity requests require a bounded explicit ID batch")
        if not all(
            re.fullmatch(r"CHEMBL[0-9]+", item) for item in values[0].split(",")
        ):
            raise SnapshotError("Invalid ChEMBL ID")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SnapshotError(
            "Unexpected redirect; update the verified official endpoint"
        )


def retry_delay(error, attempt):
    minimum = (5, 15, 60)[min(attempt, 2)]
    if not isinstance(error, HTTPError):
        return minimum
    value = error.headers.get("Retry-After", "") if error.headers else ""
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return minimum
    if delay > 3600:
        raise SnapshotError("Source requested a wait over one hour; resume later")
    return max(minimum, delay)


class SnapshotHTTP:
    def __init__(
        self,
        directory,
        *,
        max_bytes=MAX_BYTES,
        max_requests=MAX_REQUESTS,
        transport=None,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "raw").mkdir(exist_ok=True)
        self.path = self.directory / "checkpoint.json"
        self.transport = transport
        self.state = (
            load_json(self.path.read_bytes())
            if self.path.exists()
            else {
                "schema": SCHEMA,
                "created_at": now(),
                "budget": {"max_bytes": max_bytes, "max_requests": max_requests},
                "attempts": [],
                "cache": {},
            }
        )
        if self.state.get("schema") != SCHEMA or self.state["budget"] != {
            "max_bytes": max_bytes,
            "max_requests": max_requests,
        }:
            raise SnapshotError(
                "Checkpoint configuration changed; use a new snapshot directory"
            )
        self.used_sources = {}

    def save(self):
        write_json(self.path, self.state)

    def read(self, url, limit, attempt):
        # Reservation stays on disk until finally saved. In-process failures
        # charge bytes actually read; process death keeps the full reservation.
        attempt["charged_bytes"] = 0
        if self.transport:
            data = self.transport(url, limit)
            attempt["charged_bytes"] = len(data)
            if len(data) > limit:
                raise SnapshotError("Response exceeds download limit")
            return data
        request = Request(
            url,
            headers={
                "User-Agent": "biomed/1.0 (bounded-standard-resources)",
                "Accept-Encoding": "identity",
                "Accept": "application/json",
            },
        )
        started = time.monotonic()
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            if response.headers.get("Content-Encoding", "identity") not in (
                "",
                "identity",
            ):
                raise SnapshotError("Unexpected compression for identity request")
            chunks = []
            received = 0
            while True:
                if time.monotonic() - started > 180:
                    raise TimeoutError("Source read exceeded 180 seconds")
                if received >= limit:
                    raise SnapshotError("Response reached download limit")
                chunk = response.read(min(16384, limit - received))
                if not chunk:
                    break
                received += len(chunk)
                attempt["charged_bytes"] = received
                chunks.append(chunk)
                time.sleep(max(0, received / RATE - (time.monotonic() - started)))
            return b"".join(chunks)

    def fetch(self, url, *, fresh=False):
        allowed(url)
        cached = self.state["cache"].get(url)
        if cached and not fresh:
            data = (self.directory / cached["file"]).read_bytes()
            if sha256(data) != cached["sha256"]:
                raise SnapshotError("Cached source checksum mismatch")
            self.used_sources[url] = cached
            return load_json(data)
        for retry in range(3):
            attempts = self.state["attempts"]
            budget = self.state["budget"]
            remaining = budget["max_bytes"] - sum(x["charged_bytes"] for x in attempts)
            if remaining <= 0 or len(attempts) >= budget["max_requests"]:
                raise SnapshotError(
                    "Snapshot download budget exhausted; checkpoint retained"
                )
            if attempts:
                elapsed = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(attempts[-1]["at"])
                ).total_seconds()
                time.sleep(max(0, INTERVAL - elapsed))
            limit = min(RESPONSE_LIMIT, remaining)
            attempt = {
                "url": url,
                "at": now(),
                "charged_bytes": limit,
                "state": "started",
            }
            attempts.append(attempt)
            self.save()  # An interrupted request retains its conservative reservation.
            try:
                data = self.read(url, limit, attempt)
                value = load_json(data)
                digest = sha256(data)
                filename = f"raw/{digest}.json"
                path = self.directory / filename
                if not path.exists():
                    temporary = path.with_suffix(".tmp")
                    temporary.write_bytes(data)
                    os.replace(temporary, path)
                receipt = {
                    "url": url,
                    "file": filename,
                    "sha256": digest,
                    "bytes": len(data),
                    "fetched_at": now(),
                }
                attempt.update(state="completed", charged_bytes=len(data), **receipt)
                self.state["cache"][url] = receipt
                self.used_sources[url] = receipt
                self.save()
                return value
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                IncompleteRead,
                RemoteDisconnected,
            ) as exc:
                attempt.update(state="error", error=type(exc).__name__)
                self.save()
                retryable = not isinstance(exc, HTTPError) or exc.code in (
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                if not retryable or retry == 2:
                    raise SnapshotError(
                        f"Source request failed: {type(exc).__name__}; checkpoint retained"
                    ) from exc
                time.sleep(retry_delay(exc, retry))
            except Exception:
                attempt["state"] = "error"
                self.save()
                raise


def status_version(status):
    if not isinstance(status, dict) or status.get("status") != "UP":
        raise SnapshotError("ChEMBL source status is not UP")
    version, date = status.get("chembl_db_version"), status.get("chembl_release_date")
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"ChEMBL_[0-9]+", version)
        or not isinstance(date, str)
    ):
        raise SnapshotError("Missing source version or release date")
    return {"version": version, "release_date": date}


def pages(client, endpoint, params=None):
    collection, id_key = COLLECTIONS[endpoint]
    params = dict(params or {})
    rows, seen, total, offset = [], set(), None, 0
    while True:
        url = (
            API
            + endpoint
            + ".json?"
            + urlencode(
                {**params, "limit": PAGE_SIZE, "offset": offset, "order_by": id_key}
            )
        )
        result = client.fetch(url)
        meta = result.get("page_meta") if isinstance(result, dict) else None
        batch = result.get(collection) if isinstance(result, dict) else None
        if not isinstance(meta, dict) or not isinstance(batch, list):
            raise SnapshotError("Missing source pagination metadata")
        count = meta.get("total_count")
        if (
            type(count) is not int
            or count < 0
            or meta.get("offset") != offset
            or meta.get("limit") != PAGE_SIZE
        ):
            raise SnapshotError("Invalid source pagination metadata")
        if total is None:
            total = count
        if total != count or len(batch) != min(PAGE_SIZE, total - offset):
            raise SnapshotError("Changed total or incomplete source page")
        for row in batch:
            if not isinstance(row, dict):
                raise SnapshotError("Invalid source record")
            identifier = row.get(id_key)
            valid_id = (
                type(identifier) is int and identifier > 0
                if endpoint == "mechanism"
                else isinstance(identifier, str)
                and bool(re.fullmatch(r"CHEMBL[0-9]+", identifier))
            )
            if not valid_id or identifier in seen:
                raise SnapshotError("Missing or duplicate source identifier")
            seen.add(identifier)
            # Keep source fields untouched; provenance is a separate reserved field.
            rows.append({**row, "_source": dict(client.used_sources[url])})
        offset += len(batch)
        if bool(meta.get("next")) != (offset < total):
            raise SnapshotError("Source next-page marker contradicts count")
        if offset == total:
            break
    return rows


def entity_batches(client, endpoint, identifiers):
    identifiers = sorted(set(identifiers))
    result = []
    for start in range(0, len(identifiers), BATCH_SIZE):
        batch = identifiers[start : start + BATCH_SIZE]
        if not all(
            isinstance(item, str) and re.fullmatch(r"CHEMBL[0-9]+", item)
            for item in batch
        ):
            raise SnapshotError("Referenced entity has invalid ChEMBL ID")
        rows = pages(client, endpoint, {f"{endpoint}_chembl_id__in": ",".join(batch)})
        if {row[f"{endpoint}_chembl_id"] for row in rows} != set(batch):
            raise SnapshotError(
                "Referenced entity batch is incomplete or contains unrequested IDs"
            )
        result.extend(rows)
    return result


def validate_completed(directory):
    manifest = load_json((directory / "manifest.json").read_bytes())
    if manifest.get("schema") != SCHEMA or manifest.get("state") != "completed":
        raise SnapshotError("Snapshot is incomplete")
    for receipt in [*manifest["files"].values(), *manifest["sources"]]:
        path = directory / receipt["file"]
        if (
            not path.resolve().is_relative_to(directory.resolve())
            or sha256(path.read_bytes()) != receipt["sha256"]
        ):
            raise SnapshotError("Completed snapshot checksum mismatch")
    return manifest


def fetch_snapshot(
    output_dir, *, max_bytes=MAX_BYTES, max_requests=MAX_REQUESTS, transport=None
):
    """Fetch all mechanisms, only their referenced molecules/parents and targets.

    Re-running an interrupted directory reuses verified responses. A completed
    directory is immutable and only locally verified, never refreshed in place.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError("Another process owns this snapshot") from exc
        if (directory / "manifest.json").exists():
            return validate_completed(directory)
        client = SnapshotHTTP(
            directory,
            max_bytes=max_bytes,
            max_requests=max_requests,
            transport=transport,
        )
        try:
            before = client.fetch(API + "status.json", fresh=True)
            version = status_version(before)
            if client.state.get("version", version) != version:
                raise SnapshotError(
                    "Source version changed since checkpoint; use a new directory"
                )
            client.state.update(version=version, state="running")
            client.state.pop("error", None)
            client.state.pop("failed_at", None)
            client.save()
            before_receipt = dict(client.used_sources[API + "status.json"])
            mechanisms = pages(client, "mechanism")
            if not mechanisms:
                raise SnapshotError("Empty mechanism source cannot become a release")
            molecules_needed = {row["molecule_chembl_id"] for row in mechanisms}
            molecules_needed.update(
                row["parent_molecule_chembl_id"]
                for row in mechanisms
                if row.get("parent_molecule_chembl_id")
            )
            molecules = entity_batches(client, "molecule", molecules_needed)
            for _ in range(4):
                known = {row["molecule_chembl_id"] for row in molecules}
                parents = {
                    (row.get("molecule_hierarchy") or {}).get("parent_chembl_id")
                    for row in molecules
                } - {None}
                if not parents - known:
                    break
                molecules.extend(entity_batches(client, "molecule", parents - known))
            else:
                raise SnapshotError("Unclosed parent molecule hierarchy")
            targets_needed = {
                row["target_chembl_id"]
                for row in mechanisms
                if row.get("target_chembl_id")
            }
            targets = entity_batches(client, "target", targets_needed)
            after = client.fetch(API + "status.json", fresh=True)
            if status_version(after) != version:
                raise SnapshotError(
                    "Source version changed during snapshot; publication blocked"
                )
            files = {}
            for name, rows in (
                ("mechanisms", mechanisms),
                ("molecules", molecules),
                ("targets", targets),
            ):
                receipt = write_json(directory / f"{name}.json", rows)
                files[name] = {**receipt, "count": len(rows)}
            cjk = re.compile(r"[\u3400-\u9fff\U00020000-\U0003134f]")
            cjk_aliases = [
                row["molecule_chembl_id"]
                for row in molecules
                for alias in row.get("molecule_synonyms") or []
                if any(
                    cjk.search(str(alias.get(key) or ""))
                    for key in ("molecule_synonym", "synonyms")
                )
            ]
            manifest = {
                "schema": SCHEMA,
                "state": "completed",
                "source": "ChEMBL",
                **version,
                "created_at": client.state["created_at"],
                "completed_at": now(),
                "license": LICENSE,
                "source_url": API,
                "scope": "all mechanisms plus referenced molecules, their parents and targets; no activities or indications",
                "status_before": before,
                "status_after": after,
                "status_before_source": before_receipt,
                "files": files,
                "counts": {name: value["count"] for name, value in files.items()},
                "sources": list(client.used_sources.values()),
                "limits": {
                    **client.state["budget"],
                    "interval_seconds": INTERVAL,
                    "bytes_per_second": RATE,
                },
                "charged_bytes": sum(
                    x["charged_bytes"] for x in client.state["attempts"]
                ),
                "request_attempts": len(client.state["attempts"]),
                "clinical_efficacy_verified": False,
                "cjk_synonym_records": len(cjk_aliases),
                "cjk_synonym_molecules": len(set(cjk_aliases)),
                "cjk_synonym_note": "Source strings containing CJK ideographs; language and synonym scope remain unverified, no generated translations",
            }
            # Preserve both original status responses, even when their bytes differ.
            if before_receipt not in manifest["sources"]:
                manifest["sources"].append(before_receipt)
            write_json(directory / "manifest.json", manifest)
            client.state.update(
                state="completed", completed_at=manifest["completed_at"]
            )
            client.save()
            return manifest
        except Exception as exc:
            client.state.update(state="paused", error=str(exc), failed_at=now())
            client.save()
            raise
