#!/usr/bin/env python3
"""Register immutable reference files with versions and hashes; no network calls.

Plans are JSON objects with an ``entries`` array. The manifest is authoritative;
catalog.tsv is a derived, human-readable index. Sources are never removed.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile


DEFAULT_ROOT = Path("/appdata/02_resource/databases")
IDENTITY = ("provider", "dataset", "assembly", "assembly_accession", "annotation_release", "source_url")
COLUMNS = ("relative_path", *IDENTITY, "bytes", "sha256", "upstream_md5", "retrieved_at", "registered_at")
RESERVED = {"manifest.json", "catalog.tsv", ".catalog.lock"}


class CatalogError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def target_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise CatalogError("relative_path must be a nonempty POSIX relative path")
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != relative or path.parts[0] in RESERVED:
        raise CatalogError("Unsafe or reserved relative_path: " + relative)
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise CatalogError("Path escapes catalog root or is a symlink: " + relative)
    return target


def hashes(path):
    sha256, md5 = hashlib.sha256(), hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


def read_manifest(root):
    path = root / "manifest.json"
    if not path.exists():
        return {"schema_version": 1, "files": {}}
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1 or not isinstance(value.get("files"), dict):
        raise CatalogError("Unsupported manifest schema")
    return value


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def tsv(manifest):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, delimiter="\t", extrasaction="ignore")
    writer.writeheader()
    for relative, record in sorted(manifest["files"].items()):
        writer.writerow({"relative_path": relative, **record})
    return output.getvalue()


def prepare_entry(root, entry, records):
    relative = entry["relative_path"]
    destination = target_path(root, relative)
    source = Path(entry["source_path"]).resolve(strict=True)
    if not source.is_file():
        raise CatalogError("Not a regular source file: " + str(source))
    for key in IDENTITY:
        if key == "annotation_release" and entry.get(key) is None:
            continue
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise CatalogError("Missing identity field: " + key)
    extra = entry.get("extra", {})
    if not isinstance(extra, dict):
        raise CatalogError("extra must be an object")
    size, sha256, md5 = hashes(source)
    expected_md5 = entry.get("upstream_md5")
    if expected_md5 is not None and (not isinstance(expected_md5, str) or expected_md5.lower() != md5):
        raise CatalogError("Upstream MD5 mismatch: " + relative)
    record = {key: entry.get(key) for key in IDENTITY}
    record.update(bytes=size, sha256=sha256, upstream_md5=expected_md5.lower() if expected_md5 else None,
                  retrieved_at=entry.get("retrieved_at"), registered_at=now(), extra=extra,
                  source_path=str(source), source_mtime_ns=source.stat().st_mtime_ns)
    existing = records.get(relative)
    if existing:
        if any(existing.get(key) != record[key] for key in (*IDENTITY, "bytes", "sha256", "extra")):
            raise CatalogError("Existing metadata/content conflicts: " + relative)
        record = existing  # Keep first provenance and registration time intact.
    if destination.exists() and hashes(destination)[:2] != (size, sha256):
        raise CatalogError("Destination differs; refusing overwrite: " + relative)
    return relative, source, destination, record


def publish_file(source, destination, expected):
    """Publish without replacing an existing name, on either filesystem."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ("." + destination.name + ".import-" + os.urandom(8).hex())
    try:
        try:
            os.link(source, temporary)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP):
                raise
            shutil.copyfile(source, temporary)
        if hashes(temporary)[:2] != expected:
            raise CatalogError("Source changed while importing: " + str(source))
        os.link(temporary, destination)  # Exclusive publication: no clobber.
    finally:
        temporary.unlink(missing_ok=True)


def import_plan(root, plan):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("Plan must contain a nonempty entries array")
    with (root / ".catalog.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = read_manifest(root)
        records = dict(manifest["files"])
        prepared = []
        for entry in entries:
            item = prepare_entry(root, entry, records)
            records[item[0]] = item[3]
            prepared.append(item)
        created = []
        try:
            for relative, source, destination, record in prepared:
                if destination.exists():
                    continue
                # Reuse a verified catalog blob when available, even across source filesystems.
                for other, other_record in records.items():
                    if other != relative and other_record["sha256"] == record["sha256"]:
                        candidate = target_path(root, other)
                        if candidate.is_file() and hashes(candidate)[:2] == (record["bytes"], record["sha256"]):
                            source = candidate
                            break
                publish_file(source, destination, (record["bytes"], record["sha256"]))
                created.append(destination)
            updated = {"schema_version": 1, "files": records}
            atomic_write(root / "manifest.json", json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
        atomic_write(root / "catalog.tsv", tsv(updated))
        return {"entries": len(entries), "new_files": len(created), "registered_files": len(records)}


def verify(root):
    root = Path(root).resolve()
    manifest = read_manifest(root)
    for relative, record in manifest["files"].items():
        size, sha256, md5 = hashes(target_path(root, relative))
        if (size, sha256) != (record["bytes"], record["sha256"]) or (record.get("upstream_md5") and md5 != record["upstream_md5"]):
            raise CatalogError("Verification failed: " + relative)
    return {"verified_files": len(manifest["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "list", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        if name == "import":
            command.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "import":
            result = import_plan(args.root, json.loads(args.plan.read_text()))
        elif args.command == "verify":
            result = verify(args.root)
        else:
            print(tsv(read_manifest(args.root.resolve())), end="")
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CatalogError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("Reference catalog: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
