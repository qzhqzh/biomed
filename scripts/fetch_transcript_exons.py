"""Fetch exact human RefSeq RNA versions in bounded, resumable EFetch batches.

The original multi-record responses and their hashes are retained. This command
does not infer exon counts, publish a knowledge release, or call a model.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.annotations.knowledge.transcript_structures import (
    REFSEQ,
    StructureImportError,
    _genbank_records,
)

ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class FetchError(ValueError):
    pass


class BudgetReached(FetchError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path, payload):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def official_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "eutils.ncbi.nlm.nih.gov"
        and parsed.path == "/entrez/eutils/efetch.fcgi"
        and not parsed.fragment
    )


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official_url(newurl):
            raise FetchError("Refusing an EFetch redirect outside the official HTTPS endpoint")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_accessions(path, *, mane=False):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        if mane:
            reader = csv.DictReader(handle, delimiter="\t")
            if "RefSeq_nuc" not in (reader.fieldnames or []):
                raise FetchError("MANE input must contain RefSeq_nuc")
            values = [row["RefSeq_nuc"].strip() for row in reader]
        else:
            values = []
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                value = next(csv.reader([line], delimiter="\t" if "\t" in line else ","))[0].strip()
                if value.lower() in ("accession", "transcript", "refseq_nuc", "transcript_id"):
                    continue
                values.append(value)
    invalid = [value for value in values if not REFSEQ.fullmatch(value)]
    if invalid:
        raise FetchError(f"Only complete NM_/NR_ versions are allowed: {invalid[0]}")
    accessions = list(dict.fromkeys(values))
    if not accessions:
        raise FetchError("Input contains no exact transcript versions")
    return accessions


def validate_response(payload, requested):
    """Validate identity, human origin, RNA structure and every record terminator."""
    try:
        parsed = _genbank_records(payload.decode("utf-8"))
    except (UnicodeError, StructureImportError) as error:
        raise FetchError(f"Invalid GenBank response: {error}") from error
    versions = [record["accession"] for record in parsed]
    if len(versions) != len(set(versions)):
        raise FetchError("Duplicate VERSION in GenBank batch")
    unexpected, missing = set(versions) - set(requested), set(requested) - set(versions)
    if unexpected or missing:
        raise FetchError(f"Exact VERSION mismatch; unexpected={sorted(unexpected)}, missing={sorted(missing)}")
    records, start = [], 0
    for record, end_match in zip(parsed, re.finditer(rb"(?m)^//[ \t]*(?:\r?\n|$)", payload)):
        end = end_match.end()
        raw = payload[start:end]
        records.append({"accession": record["accession"], "byte_start": start, "byte_end": end,
                        "sha256": sha256(raw), "bytes": len(raw)})
        start = end
    if len(records) != len(parsed) or payload[start:].strip():
        raise FetchError("Unable to delimit all original GenBank records")
    return records


def retry_after(value):
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return 0
    if not math.isfinite(delay) or delay > 3600:
        raise FetchError("Retry-After is invalid or exceeds one hour; paused without another request")
    return max(0, delay)


class Downloader:
    def __init__(self, *, max_bytes, interval=2, bytes_per_second=131072,
                 opener=None, sleep=time.sleep, monotonic=time.monotonic):
        if max_bytes <= 0 or interval < 2 or not 1 <= bytes_per_second <= 262144:
            raise FetchError("Require positive byte budget, interval >= 2s, and rate 1..262144 B/s")
        self.max_bytes, self.interval, self.rate = max_bytes, interval, bytes_per_second
        self.opener = opener or build_opener(OfficialRedirect()).open
        self.sleep, self.monotonic = sleep, monotonic
        self.bytes = self.requests = 0
        self.last_start = None

    def fetch(self, accessions):
        url = ENDPOINT + "?" + urlencode({"db": "nuccore", "id": ",".join(accessions),
                                         "rettype": "gb", "retmode": "text", "tool": "biomed_transcript_exons"})
        for attempt in range(4):
            if self.bytes >= self.max_bytes:
                raise BudgetReached("Network response byte budget reached")
            if self.last_start is not None:
                delay = self.interval - (self.monotonic() - self.last_start)
                if delay > 0:
                    self.sleep(delay)
            self.last_start = self.monotonic()
            self.requests += 1
            try:
                request = Request(url, headers={"User-Agent": "biomed-transcript-exons/1.0", "Accept-Encoding": "identity"})
                with self.opener(request, timeout=60) as response:
                    if not official_url(response.geturl()):
                        raise FetchError("Response URL is not the official EFetch endpoint")
                    length = response.headers.get("Content-Length")
                    expected = int(length) if length and length.isdigit() else None
                    if expected is not None and expected > self.max_bytes - self.bytes:
                        raise BudgetReached("Response exceeds remaining byte budget")
                    parts, received, started = [], 0, self.monotonic()
                    while True:
                        allowance = self.max_bytes - self.bytes
                        if allowance <= 0:
                            if expected is not None and received == expected:
                                break
                            raise BudgetReached("Byte budget reached before complete response")
                        try:
                            block = response.read(min(8192, allowance))
                        except IncompleteRead as error:
                            self.bytes += len(error.partial)
                            received += len(error.partial)
                            delay = received / self.rate - (self.monotonic() - started)
                            if delay > 0:
                                self.sleep(delay)
                            raise
                        if not block:
                            break
                        self.bytes += len(block)
                        received += len(block)
                        parts.append(block)
                        delay = received / self.rate - (self.monotonic() - started)
                        if delay > 0:
                            self.sleep(delay)
                    if expected is not None and received != expected:
                        raise OSError("Truncated HTTP response")
                    return b"".join(parts), url
            except HTTPError as error:
                transient = error.code in (408, 429, 500, 502, 503, 504)
                if not transient or attempt == 3:
                    raise FetchError(f"EFetch HTTP {error.code}; batch remains pending") from error
                self.sleep(max(2 ** (attempt + 1), retry_after(error.headers.get("Retry-After"))))
            except (URLError, TimeoutError, ConnectionError, OSError, IncompleteRead) as error:
                if attempt == 3:
                    raise FetchError(f"EFetch connection failed after 4 attempts: {type(error).__name__}") from error
                self.sleep(2 ** (attempt + 1))
        raise AssertionError("Unreachable")


def source_accessions(source):
    expected = source.get("accessions") or ([source["accession"]] if source.get("accession") else [])
    if not expected:
        expected = parse_qs(urlsplit(source.get("url", "")).query).get("id", [""])[0].split(",")
    if not expected or any(not REFSEQ.fullmatch(value) for value in expected) or len(expected) != len(set(expected)):
        raise FetchError("Cache source has no trustworthy exact requested versions")
    return expected


def reuse_sources(manifest_path, output, sources, records):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    existing = {source["sha256"] for source in sources}
    for original in manifest.get("sources", []):
        if original.get("sha256") in existing:
            continue
        if not official_url(original.get("url", "")):
            raise FetchError("Cache source URL is not the official EFetch endpoint")
        path = Path(original["path"])
        if not path.is_absolute():
            path = Path(manifest_path).parent / path
        if not path.exists():
            # Seed snapshots may have been relocated without rewriting their paths.
            path = Path(manifest_path).parent / Path(original["path"]).name
        payload = path.read_bytes()
        if sha256(payload) != original.get("sha256") or len(payload) != original.get("bytes"):
            raise FetchError(f"Cache hash/size mismatch: {path.name}")
        parsed = validate_response(payload, source_accessions(original))
        target = output / f"batch-{sha256(payload)}.gb"
        if target.exists():
            if sha256(target.read_bytes()) != sha256(payload):
                raise FetchError(f"Existing output cache hash mismatch: {target.name}")
        else:
            atomic_bytes(target, payload)
        source = {**original, "path": str(target), "accessions": [item["accession"] for item in parsed]}
        sources.append(source)
        existing.add(source["sha256"])
        for item in parsed:
            records.append({**item, "path": str(target), "url": source["url"],
                            "response_sha256": source["sha256"], "response_bytes": source["bytes"],
                            "observed_at": source.get("observed_at"), "retrieved_at": source.get("retrieved_at")})


def fetch_batches(accessions, output, *, batch_size=20, max_records=100, max_bytes=50 * 1024 * 1024,
                  interval=2, bytes_per_second=131072, reuse_manifests=(), downloader=None):
    if not 1 <= batch_size <= 100 or max_records < 0:
        raise FetchError("Require batch-size 1..100 and a nonnegative new-record budget")
    if not accessions or len(accessions) != len(set(accessions)) or any(not REFSEQ.fullmatch(value) for value in accessions):
        raise FetchError("Requested accessions must be unique complete NM_/NR_ versions")
    client = downloader or Downloader(max_bytes=max_bytes, interval=interval, bytes_per_second=bytes_per_second)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".fetch.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FetchError("Another transcript fetch process owns this output directory") from error
        sources, records, errors = [], [], []
        manifest_path = output / "sources.json"
        try:
            if manifest_path.exists():
                reuse_sources(manifest_path, output, sources, records)
            for path in reuse_manifests:
                reuse_sources(path, output, sources, records)
        except (FetchError, OSError, ValueError, KeyError) as error:
            atomic_json(output / "status.json", {"state": "paused_cache_error", "error": str(error),
                                                "complete": False, "updated_at": now()})
            raise
        requested = set(accessions)
        known = {record["accession"] for record in records}
        cached = len(requested & known)
        pending = [accession for accession in accessions if accession not in known]
        fetched, state = 0, "running"

        def checkpoint():
            completed = len(requested & {record["accession"] for record in records})
            summary = {"state": state, "requested": len(accessions), "completed": completed,
                       "cache_reused": cached, "new_records": fetched, "failed": len(errors),
                       "pending": len(accessions) - completed, "network_response_bytes": client.bytes,
                       "requests": client.requests, "updated_at": now(), "complete": completed == len(accessions)}
            manifest = {"schema_version": 2, "sources": sources, "records": records, "errors": errors,
                        "limits": {"batch_size": batch_size, "max_records": max_records, "max_bytes": max_bytes,
                                   "interval_seconds": interval, "bytes_per_second": bytes_per_second}, **summary}
            atomic_json(manifest_path, manifest)
            atomic_json(output / "status.json", summary)
            return summary

        checkpoint()
        try:
            while pending and fetched < max_records:
                batch = pending[:min(batch_size, max_records - fetched)]
                try:
                    payload, url = client.fetch(batch)
                    parsed = validate_response(payload, batch)
                except BudgetReached:
                    state = "paused_byte_budget"
                    break
                except FetchError as error:
                    errors.extend({"accession": accession, "error": str(error), "observed_at": now()} for accession in batch)
                    state = "paused_error"
                    break
                retrieved = now()
                digest = sha256(payload)
                target = output / f"batch-{digest}.gb"
                atomic_bytes(target, payload)
                source = {"path": str(target), "url": url, "sha256": digest, "bytes": len(payload),
                          "accessions": batch, "observed_at": retrieved, "retrieved_at": retrieved,
                          "time_basis": "response_fully_received_at", "name": "NCBI RefSeq GenBank"}
                sources.append(source)
                for item in parsed:
                    records.append({**item, "path": str(target), "url": url, "response_sha256": digest,
                                    "response_bytes": len(payload), "observed_at": retrieved, "retrieved_at": retrieved})
                fetched += len(parsed)
                pending = pending[len(batch):]
                checkpoint()
            if state == "running":
                state = "complete" if not pending else "paused_record_budget"
        except KeyboardInterrupt:
            state = "interrupted"
        except Exception:
            # A programming/filesystem failure must never leave a false running state.
            state = "paused_error"
            raise
        finally:
            summary = checkpoint()
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--accessions", type=Path, help="Exact version per line, or CSV/TSV first column")
    source.add_argument("--mane", type=Path, help="MANE summary (.txt or .txt.gz), RefSeq_nuc column")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reuse-manifest", "--cache-manifest", action="append", default=[], type=Path)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-records", type=int, default=100, help="Maximum new records in this invocation")
    parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024, help="All response bytes this invocation, including retries")
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--bytes-per-second", type=int, default=131072)
    args = parser.parse_args(argv)
    try:
        result = fetch_batches(load_accessions(args.mane or args.accessions, mane=bool(args.mane)), args.output,
                               batch_size=args.batch_size, max_records=args.max_records, max_bytes=args.max_bytes,
                               interval=args.interval, bytes_per_second=args.bytes_per_second,
                               reuse_manifests=args.reuse_manifest)
    except (FetchError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"state": "error", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["state"] in ("paused_error", "interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
