#!/usr/bin/env python3
"""Prepare this experiment's Python 3.11 runtime from verified local wheel archives.

This is an experimental archive-layout installation, not a customer delivery method
or a replacement for a wheel installer. Console entry points are not generated and
wheel .data directories retain their archive layout. No source files are modified.
Do not edit hard-linked package files: they share inodes with the source cache.
Cache unlinking does not remove these independent directory entries. Use a new
environment for changes, then verify it with uv pip check and imports.

--check-only performs all preflight checks without creating files or directories.
An interrupted preparation can resume: existing identical planned files are reused;
unrelated packages, symlinks, and differing destination files are always rejected.
"""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "var/runtime"
CACHE_ROOT = Path("/data/models/.uv-cache/archive-v0")
EXPECTED_PACKAGES = 45
BOOTSTRAP_FILES = {"_virtualenv.pth", "_virtualenv.py", "distutils-precedence.pth"}
BOOTSTRAP_PACKAGES = {"pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack"}


def normalized_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def absolute_path(value):
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is forbidden: {path}")
    return path.absolute()


def directory_fd(path):
    """Open every path component without following a symbolic link."""
    path = absolute_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def regular_file(path):
    fd = directory_fd(path.parent)
    try:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file, not a symlink: {path}")
    return info


def sha256(path):
    regular_file(path)
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def relative_record(value):
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive-relative path: {value!r}")
    if not path.parts or str(path) != value:
        raise ValueError(f"Noncanonical archive-relative path: {value!r}")
    return Path(*path.parts)


def read_requirements(path):
    regular_file(path)
    package = archive = None
    result = []
    for line in path.read_text().splitlines():
        line = line.strip()
        header = re.fullmatch(r"# ([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)", line)
        if header:
            if package or archive:
                raise ValueError("Incomplete requirement/archive block")
            package = (normalized_name(header[1]), header[2])
        elif line.startswith("# archive: "):
            if archive is not None:
                raise ValueError("Duplicate archive annotation")
            archive = absolute_path(line.removeprefix("# archive: "))
        elif line and not line.startswith("#"):
            requirement = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s+(\S+)", line
            )
            if not requirement or not package or archive is None:
                raise ValueError(f"Unsupported or unannotated requirement: {line}")
            name, version = package
            if normalized_name(requirement[1]) != name or archive.parent != CACHE_ROOT:
                raise ValueError(
                    "Requirement identity or permitted cache root mismatch"
                )
            url = urlsplit(requirement[2])
            wheel = unquote(PurePosixPath(url.path).name).split("-")
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or len(wheel) < 5
                or not wheel[-1].endswith(".whl")
                or normalized_name(wheel[0]) != name
                or wheel[1] != version
            ):
                raise ValueError(f"Wheel URL does not identify {name}=={version}")
            result.append(
                {
                    "name": name,
                    "version": version,
                    "archive": archive,
                    "url": requirement[2],
                }
            )
            package = archive = None
    if package or archive or len(result) != EXPECTED_PACKAGES:
        raise ValueError(f"Expected {EXPECTED_PACKAGES} complete package blocks")
    if len({item["name"] for item in result}) != len(result):
        raise ValueError("Duplicate package names")
    if len({item["archive"] for item in result}) != len(result):
        raise ValueError("Duplicate source archives")
    return result


def walk_plain(root):
    fd = directory_fd(root)
    os.close(fd)
    files, directories = {}, set()
    for current, names, filenames in os.walk(root, followlinks=False):
        current = Path(current)
        for name in names:
            path = current / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError(f"Symlink or special directory is forbidden: {path}")
            directories.add(path.relative_to(root))
        for name in filenames:
            path = current / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Symlink or special file is forbidden: {path}")
            files[path.relative_to(root)] = info
    return files, directories


def compatible_tag(tag):
    parts = tag.split("-")
    if len(parts) != 3:
        return False
    interpreter, abi, platform = parts
    python_ok = (
        interpreter in {"py3", "py2.py3"}
        and abi == "none"
        or interpreter == "cp311"
        and abi in {"cp311", "abi3", "none"}
    )
    if abi == "abi3" and re.fullmatch(r"cp3\d+", interpreter):
        python_ok = int(interpreter[3:]) <= 11
    return python_ok and (
        platform in {"any", "linux_x86_64"}
        or platform.startswith("manylinux")
        and platform.endswith("_x86_64")
    )


def same_file_content(left, right):
    a, b = regular_file(left), regular_file(right)
    return a.st_size == b.st_size and (
        (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) or sha256(left) == sha256(right)
    )


def build_plan(packages, site_packages):
    files, directories, manifests = {}, set(), []
    target_device = site_packages.stat().st_dev
    for item in packages:
        root = item["archive"]
        source_files, source_dirs = walk_plain(root)
        if root.stat().st_uid != os.getuid():
            raise ValueError(f"Source archive is not owned by this user: {root}")
        metadata_paths = [
            p
            for p in source_files
            if len(p.parts) == 2
            and p.name == "METADATA"
            and p.parent.name.endswith(".dist-info")
        ]
        if len(metadata_paths) != 1:
            raise ValueError(
                f"Expected exactly one distribution metadata directory: {root}"
            )
        metadata_path = root / metadata_paths[0]
        metadata = Parser().parsestr(metadata_path.read_text())
        if (
            normalized_name(metadata.get("Name", "")) != item["name"]
            or metadata.get("Version") != item["version"]
        ):
            raise ValueError(f"Archive metadata identity mismatch: {root}")
        wheel_path = metadata_path.with_name("WHEEL")
        record_path = metadata_path.with_name("RECORD")
        regular_file(wheel_path)
        regular_file(record_path)
        wheel = Parser().parsestr(wheel_path.read_text())
        if not any(compatible_tag(tag) for tag in wheel.get_all("Tag", [])):
            raise ValueError(
                f"Archive has no compatible CPython 3.11/Linux x86_64 tag: {root}"
            )
        recorded = set()
        with record_path.open(newline="") as stream:
            for row in csv.reader(stream):
                if len(row) != 3:
                    raise ValueError(f"Invalid wheel RECORD: {record_path}")
                relative = relative_record(row[0])
                if relative in recorded or relative not in source_files:
                    raise ValueError(
                        f"Duplicate or missing RECORD entry: {root / relative}"
                    )
                recorded.add(relative)
                if row[2] and source_files[relative].st_size != int(row[2]):
                    raise ValueError(f"RECORD size mismatch: {root / relative}")
        if recorded != set(source_files):
            raise ValueError(f"Unrecorded source files in {root}")
        for relative, info in source_files.items():
            source = root / relative
            if relative.suffix == ".pth":
                raise ValueError(
                    f"Source startup .pth files are not supported: {source}"
                )
            if info.st_dev != target_device:
                raise ValueError(
                    f"Hard links require source and target on one filesystem: {source}"
                )
            if relative in files:
                if not same_file_content(files[relative][0], source):
                    raise ValueError(
                        f"Packages contain conflicting shared file: {relative}"
                    )
            else:
                files[relative] = (source, info)
        directories.update(source_dirs)
        manifests.append(
            {
                **item,
                "archive": str(root),
                "metadata_sha256": sha256(metadata_path),
                "record_sha256": sha256(record_path),
            }
        )
    if set(files) & directories:
        raise ValueError("Package files conflict with package directories")
    return files, directories, manifests


def bootstrap_path(relative):
    top = relative.parts[0]
    return (
        top in BOOTSTRAP_FILES
        and len(relative.parts) == 1
        or top in BOOTSTRAP_PACKAGES
        or bool(re.fullmatch(r"(?:pip|setuptools|wheel)-[^/]+\.dist-info", top))
        or (
            top == "__pycache__"
            and (
                len(relative.parts) == 1
                or bool(
                    re.fullmatch(
                        r"_virtualenv\.cpython-311(?:\.opt-[12])?\.pyc", relative.name
                    )
                )
            )
        )
    )


def inspect_target(site_packages, files, directories):
    present, present_dirs = walk_plain(site_packages)
    for relative in present_dirs:
        if relative not in directories and not bootstrap_path(relative):
            raise ValueError(f"Unrelated target directory: {site_packages / relative}")
    reused = 0
    for relative in present:
        if relative in files:
            if not same_file_content(files[relative][0], site_packages / relative):
                raise ValueError(
                    f"Existing target file differs; refusing overwrite: {site_packages / relative}"
                )
            reused += 1
        elif not bootstrap_path(relative):
            raise ValueError(f"Unrelated target file: {site_packages / relative}")
    return reused


def parent_fd(root, relative, create=False):
    fd = directory_fd(root)
    try:
        for component in relative.parent.parts:
            if create:
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def link_files(site_packages, files):
    created = reused = 0
    for relative, (source, original) in sorted(files.items()):
        source_fd = directory_fd(source.parent)
        target_fd = parent_fd(site_packages, relative, create=True)
        try:
            current = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (
                original.st_dev,
                original.st_ino,
                original.st_size,
                original.st_mtime_ns,
            ):
                raise ValueError(f"Source changed after preflight: {source}")
            try:
                os.link(
                    source.name,
                    relative.name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=target_fd,
                    follow_symlinks=False,
                )
                created += 1
            except FileExistsError:
                if not same_file_content(source, site_packages / relative):
                    raise ValueError(
                        f"Target changed after preflight: {site_packages / relative}"
                    )
                reused += 1
        finally:
            os.close(source_fd)
            os.close(target_fd)
    return created, reused


def existing_receipt(path, plan_sha256):
    if not os.path.lexists(path):
        return False
    regular_file(path)
    receipt = json.loads(path.read_text())
    if receipt.get("plan_sha256") != plan_sha256 or receipt.get("state") != "prepared":
        raise ValueError(
            "Existing receipt belongs to another or incomplete preparation"
        )
    return True


def write_receipt(path, receipt):
    """Publish a complete receipt atomically without overwriting an existing file."""
    fd = directory_fd(path.parent)
    temporary = f".{path.name}.{os.getpid()}.tmp"
    created = False
    try:
        out = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        created = True
        with os.fdopen(out, "w") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False
        )
    finally:
        if created:
            os.unlink(temporary, dir_fd=fd)
        os.close(fd)


def run(args):
    environment = absolute_path(args.environment)
    if environment == RUNTIME_ROOT or not environment.is_relative_to(RUNTIME_ROOT):
        raise ValueError(f"Environment must be below {RUNTIME_ROOT}")
    fd = directory_fd(environment)
    os.close(fd)
    config_path = environment / "pyvenv.cfg"
    regular_file(config_path)
    config = dict(
        line.split("=", 1)
        for line in config_path.read_text().splitlines()
        if "=" in line
    )
    config = {key.strip(): value.strip() for key, value in config.items()}
    if (
        not config.get("version_info", config.get("version", "")).startswith("3.11.")
        or config.get("include-system-site-packages", "").lower() != "false"
    ):
        raise ValueError("Target must be an isolated Python 3.11 virtual environment")
    site_packages = environment / "lib/python3.11/site-packages"
    fd = directory_fd(site_packages)
    os.close(fd)
    requirements = absolute_path(args.requirements)
    receipt_path = absolute_path(
        args.receipt or environment / "pilot-runtime-receipt.json"
    )
    if not receipt_path.is_relative_to(
        PROJECT_ROOT / "var"
    ) or receipt_path.is_relative_to(site_packages):
        raise ValueError("Receipt must be below project var, outside site-packages")
    fd = directory_fd(receipt_path.parent)
    os.close(fd)
    packages = read_requirements(requirements)
    files, directories, manifests = build_plan(packages, site_packages)
    reused = inspect_target(site_packages, files, directories)
    plan = {
        "schema_version": 1,
        "environment": str(environment),
        "site_packages": str(site_packages),
        "requirements_sha256": sha256(requirements),
        "preparer_sha256": sha256(Path(__file__).absolute()),
        "packages": manifests,
        "unique_files": len(files),
        "bytes": sum(info.st_size for _, info in files.values()),
        "method": "experimental_archive_hardlinks",
        "customer_delivery": False,
        "source_validation": "metadata identity, wheel tags, RECORD membership and sizes; not full file hashes",
        "limitations": [
            "No console scripts generated",
            "Wheel .data layout is not relocated",
            "Do not edit hard-linked files",
            "Runtime imports and inference are not validated",
        ],
    }
    plan_sha256 = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    existing_receipt(receipt_path, plan_sha256)
    if args.check_only:
        return {
            "state": "preflight_passed",
            "packages": len(packages),
            "files": len(files),
            "existing_identical_files": reused,
            "bytes": plan["bytes"],
            "plan_sha256": plan_sha256,
            "writes": 0,
        }
    environment_fd = directory_fd(environment)
    lock_fd = os.open(
        ".pilot-runtime.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=environment_fd,
    )
    os.close(environment_fd)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Runtime lock must be a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inspect_target(site_packages, files, directories)
        receipt_exists = existing_receipt(receipt_path, plan_sha256)
        created, reused = link_files(site_packages, files)
        inspect_target(site_packages, files, directories)
        if not receipt_exists:
            write_receipt(
                receipt_path,
                {
                    **plan,
                    "state": "prepared",
                    "plan_sha256": plan_sha256,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_links": created,
                    "reused_files": reused,
                },
            )
        return {
            "state": "prepared",
            "packages": len(packages),
            "created_links": created,
            "reused_files": reused,
            "receipt": str(receipt_path),
            "runtime_validated": False,
        }
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--environment", "--venv", type=Path, required=True)
    parser.add_argument(
        "--receipt", type=Path, help="Receipt below project var; parent must exist"
    )
    parser.add_argument(
        "--check-only", action="store_true", help="Validate without writing any files"
    )
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False))
    except (ValueError, OSError, csv.Error) as exc:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error": str(exc),
                    "partial_target_possible": not args.check_only,
                }
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
