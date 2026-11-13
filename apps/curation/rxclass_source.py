"""Resumable, bounded RxClass FDASPL/EPC capture, independent of Django/DB.

Only the official EPC list and direct IN memberships are requested. A completed
snapshot is immutable; a changed upstream version requires a new directory.
"""

import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .network import TransientSourceError, fetch

RESOURCE_ROOT = Path("/appdata/02_resource/apps/biomed/resources")
BASE_URL = "https://rxnav.nlm.nih.gov/REST/rxclass"
CLASSES_URL = BASE_URL + "/allClasses.json?classTypes=EPC"
VERSION_URL = BASE_URL + "/version/FDASPL.json"
RXNORM_VERSION_URL = BASE_URL + "/version/RXNORM.json"
INTERVAL = 2.0
BYTES_PER_SECOND = 256 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RETRY_DELAYS = (5, 15, 45)
RIGHTS = {
    "label": "NLM RxNav terms; FDASPL/EPC subset only, excluding SNOMED CT",
    "terms_url": "https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html",
    "source_url": "https://lhncbc.nlm.nih.gov/RxNav/applications/RxClassIntro.html",
    "attribution": "U.S. National Library of Medicine; FDA SPL / MED-RT",
}


class SnapshotError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def immutable_bytes(path, content):
    if path.exists():
        if path.read_bytes() != content:
            raise SnapshotError(f"Refusing to overwrite immutable file: {path.name}")
        return
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    # A single capture holds the directory lock, so this cannot race a writer.
    temporary.replace(path)


def member_url(class_id):
    return (
        BASE_URL
        + "/classMembers.json?"
        + urlencode(
            {
                "classId": class_id,
                "relaSource": "FDASPL",
                "rela": "has_EPC",
                "trans": "1",
                "ttys": "IN",
            }
        )
    )


def parse_version(payload):
    if not isinstance(payload, dict):
        raise SnapshotError("Version response must be an object")
    version = payload.get("relaSourceVersion")
    if not isinstance(version, str) or not version.strip():
        raise SnapshotError("Missing source version")
    return version


def parse_classes(payload):
    try:
        records = payload["rxclassMinConceptList"]["rxclassMinConcept"]
    except (TypeError, KeyError) as exc:
        raise SnapshotError("Missing EPC class list") from exc
    if not isinstance(records, list) or not records:
        raise SnapshotError("Empty or invalid EPC class list")
    result = {}
    for item in records:
        if not isinstance(item, dict):
            raise SnapshotError("Invalid EPC class record")
        class_id, name = item.get("classId"), item.get("className")
        if (
            not isinstance(class_id, str)
            or not class_id
            or not isinstance(name, str)
            or not name.strip()
            or item.get("classType") != "EPC"
        ):
            raise SnapshotError("Invalid identifier, name or non-EPC class")
        if class_id in result:
            raise SnapshotError("Duplicate class ID")
        result[class_id] = {"id": class_id, "name": name, "class_type": "EPC"}
    return [result[key] for key in sorted(result)]


def parse_memberships(payload, class_id, url, raw_sha256):
    if not isinstance(payload, dict) or set(payload) - {"drugMemberGroup"}:
        raise SnapshotError("Invalid member response; not an empty result")
    # RxNav explicitly permits null/omitted empty fields in its JSON contract.
    group = payload.get("drugMemberGroup")
    if group is None:
        return []
    if not isinstance(group, dict) or set(group) - {"drugMember"}:
        raise SnapshotError("Invalid drugMemberGroup")
    members = group.get("drugMember")
    if members is None:
        return []
    if not isinstance(members, list):
        raise SnapshotError("drugMember must be a list")
    result = {}
    for member in members:
        if not isinstance(member, dict):
            raise SnapshotError("Invalid drug member")
        concept, attrs = member.get("minConcept"), member.get("nodeAttr")
        if not isinstance(concept, dict) or not isinstance(attrs, list):
            raise SnapshotError("Missing member concept or membership attributes")
        rxcui = concept.get("rxcui")
        if (
            not isinstance(rxcui, str)
            or not rxcui.isdecimal()
            or concept.get("tty") != "IN"
            or not isinstance(concept.get("name"), str)
            or not concept["name"]
        ):
            raise SnapshotError("Member is not an identified RxNorm IN ingredient")
        if any(
            not isinstance(attr, dict)
            or not isinstance(attr.get("attrName"), str)
            or not isinstance(attr.get("attrValue"), str)
            for attr in attrs
        ):
            raise SnapshotError("Invalid membership attribute")
        relations = [a["attrValue"] for a in attrs if a["attrName"] == "Relation"]
        if not relations or set(relations) != {"DIRECT"}:
            raise SnapshotError("Response violates direct-membership query")
        row = result.setdefault(
            rxcui,
            {
                "rxcui": rxcui,
                "class_id": class_id,
                "relation": "has_epc",
                "relation_source": "FDASPL",
                "direct": True,
                "source_url": url,
                "raw_sha256": raw_sha256,
                "name": concept["name"],
                "tty": "IN",
                "raw_members": [],
            },
        )
        if row["name"] != concept["name"]:
            raise SnapshotError("Conflicting names for one RxCUI within a class")
        # Multiple source concepts may map to one RxCUI. Keep all original data.
        row["raw_members"].append(member)
    return [result[key] for key in sorted(result)]


class Capture:
    def __init__(self, directory, manifest, fetcher, sleeper):
        self.directory = directory
        self.manifest = manifest
        self.fetcher = fetcher
        self.sleeper = sleeper

    def save(self):
        self.manifest["updated_at"] = now()
        atomic_json(self.directory / "manifest.json", self.manifest)

    def request(self, url, kind, parser, *, use_cache=True):
        if use_cache:
            for record in self.manifest["requests"]:
                if record["url"] == url and record["state"] == "validated":
                    raw = (self.directory / record["raw_file"]).read_bytes()
                    if sha256(raw) != record["raw_sha256"]:
                        raise SnapshotError("Cached raw response checksum mismatch")
                    return parser(json.loads(raw), record), record
        for attempt in range(len(RETRY_DELAYS) + 1):
            started = time.monotonic()
            try:
                raw = self.fetcher(
                    url,
                    limit=MAX_RESPONSE_BYTES,
                    bytes_per_second=BYTES_PER_SECOND,
                    interval=INTERVAL,
                )
            except TransientSourceError as exc:
                self.manifest["failures"].append(
                    {
                        "url": url,
                        "kind": kind,
                        "at": now(),
                        "attempt": attempt + 1,
                        "error": str(exc),
                        "retry_after_seconds": (
                            RETRY_DELAYS[attempt]
                            if attempt < len(RETRY_DELAYS)
                            else None
                        ),
                    }
                )
                self.save()
                if attempt == len(RETRY_DELAYS):
                    raise
                self.sleeper(RETRY_DELAYS[attempt])
                continue
            digest = sha256(raw)
            raw_file = f"raw/{digest}.json"
            immutable_bytes(self.directory / raw_file, raw)
            record = {
                "url": url,
                "kind": kind,
                "captured_at": now(),
                "raw_file": raw_file,
                "raw_sha256": digest,
                "response_bytes": len(raw),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "state": "received",
            }
            self.manifest["requests"].append(record)
            try:
                value = parser(json.loads(raw), record)
            except (SnapshotError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                record.update(state="invalid", error=str(exc))
                self.save()
                raise SnapshotError(f"Invalid official {kind} response: {exc}") from exc
            record["state"] = "validated"
            record["count"] = len(value) if isinstance(value, list) else 1
            self.save()
            return value, record
        raise AssertionError("Unreachable retry state")


def fetch_snapshot(output_dir, *, fetcher=fetch, sleeper=time.sleep):
    """Capture/resume one version; raise on failure without publishing partial data."""
    directory = Path(output_dir).resolve()
    if (
        not directory.is_relative_to(RESOURCE_ROOT.resolve())
        or directory == RESOURCE_ROOT
    ):
        raise SnapshotError(f"Snapshot directory must be inside {RESOURCE_ROOT}")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".capture.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError("This snapshot already has an active capture") from exc
        return _capture(directory, fetcher, sleeper)


def _capture(directory, fetcher, sleeper):
    path = directory / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text())
        if manifest.get("schema_version") != "rxclass-epc-snapshot-v1":
            raise SnapshotError("Incompatible snapshot manifest")
        if manifest["state"] == "completed":
            for filename, digest in manifest["output_sha256"].items():
                if sha256((directory / filename).read_bytes()) != digest:
                    raise SnapshotError("Completed snapshot output checksum mismatch")
            for request in manifest["requests"]:
                if (
                    sha256((directory / request["raw_file"]).read_bytes())
                    != request["raw_sha256"]
                ):
                    raise SnapshotError("Completed snapshot raw checksum mismatch")
            return manifest
    else:
        manifest = {
            "schema_version": "rxclass-epc-snapshot-v1",
            "source": "RxClass",
            "relation_source": "FDASPL",
            "class_type": "EPC",
            "relation": "has_epc",
            "direct": True,
            "member_tty": "IN",
            "rights": RIGHTS,
            "started_at": now(),
            "state": "running",
            "limits": {
                "request_interval_seconds": INTERVAL,
                "bytes_per_second": BYTES_PER_SECOND,
                "parallelism": 1,
                "retry_delays_seconds": list(RETRY_DELAYS),
            },
            "requests": [],
            "failures": [],
            "counts": {"classes_total": 0, "classes_completed": 0, "memberships": 0},
        }
    (directory / "raw").mkdir(exist_ok=True)
    capture = Capture(directory, manifest, fetcher, sleeper)
    manifest["state"] = "running"
    manifest.pop("error", None)
    capture.save()
    try:
        versions = {}
        for key, url in (("FDASPL", VERSION_URL), ("RXNORM", RXNORM_VERSION_URL)):
            versions[key], _ = capture.request(
                url,
                f"version_start_{key}",
                lambda p, _: parse_version(p),
                use_cache=False,
            )
        if "versions" in manifest and versions != manifest["versions"]:
            raise SnapshotError(
                "Upstream version changed; preserve this snapshot and use a new directory"
            )
        manifest["versions"] = versions
        manifest["version"] = versions["FDASPL"]
        capture.save()
        classes, class_request = capture.request(
            CLASSES_URL, "classes", lambda p, _: parse_classes(p)
        )
        manifest["counts"]["classes_total"] = len(classes)
        memberships = []
        empty_classes = []
        manifest["counts"]["classes_completed"] = 0
        for class_record in classes:
            class_id = class_record["id"]
            url = member_url(class_id)
            rows, _ = capture.request(
                url,
                f"members:{class_id}",
                lambda p, r, cid=class_id, source_url=url: parse_memberships(
                    p, cid, source_url, r["raw_sha256"]
                ),
            )
            memberships.extend(rows)
            if not rows:
                empty_classes.append(class_id)
            manifest["counts"]["classes_completed"] += 1
            manifest["counts"]["memberships"] = len(memberships)
            capture.save()
        final_versions = {}
        for key, url in (("FDASPL", VERSION_URL), ("RXNORM", RXNORM_VERSION_URL)):
            final_versions[key], _ = capture.request(
                url,
                f"version_end_{key}",
                lambda p, _: parse_version(p),
                use_cache=False,
            )
        if final_versions != versions:
            raise SnapshotError(
                "Upstream version changed during capture; snapshot not published"
            )
        final_classes, _ = capture.request(
            CLASSES_URL, "classes_end", lambda p, _: parse_classes(p), use_cache=False
        )
        if final_classes != classes:
            raise SnapshotError(
                "EPC class list changed during capture; snapshot not published"
            )
        if not memberships:
            raise SnapshotError(
                "All EPC membership lists are empty; refusing publication"
            )
        output = {"classes.json": classes, "memberships.json": memberships}
        for filename, value in output.items():
            immutable_bytes(directory / filename, encoded(value))
        manifest.update(
            state="completed",
            completed_at=now(),
            versions_after=final_versions,
            class_list_raw_sha256=class_request["raw_sha256"],
            empty_classes=empty_classes,
            output_sha256={
                name: sha256(encoded(value)) for name, value in output.items()
            },
        )
        manifest["counts"].update(
            ingredients=len({row["rxcui"] for row in memberships}),
            empty_classes=len(empty_classes),
            successful_responses=sum(
                r["state"] == "validated" for r in manifest["requests"]
            ),
            response_bytes=sum(r["response_bytes"] for r in manifest["requests"]),
            raw_member_rows=sum(len(row["raw_members"]) for row in memberships),
        )
        capture.save()
        return manifest
    except Exception as exc:
        manifest.update(state="paused", error=str(exc), paused_at=now())
        capture.save()
        raise
