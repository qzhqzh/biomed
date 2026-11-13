#!/usr/bin/env python3
"""Fetch pinned human RefSeq annotations, then register verified original files.

Python standard library plus curl. No database/service changes. Repeated runs reuse
the checked staging files; release versions are explicit, never inferred from mtime.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys


RELEASES = (
    ("hg19", "GRCh37.p13", "GCF_000001405.25", "GCF_000001405.25-RS_2024_09"),
    ("hg38", "GRCh38.p14", "GCF_000001405.40", "GCF_000001405.40-RS_2025_08"),
)
CHECKED_AT = "2026-10-28"
LICENSE_URL = "https://www.ncbi.nlm.nih.gov/home/about/policies/"


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hashes(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def fetch(url, path, limit, expected_md5=None):
    receipt_path = path.with_name(path.name + ".download.json")
    if path.exists():
        receipt = json.loads(receipt_path.read_text())
        sha, md5 = hashes(path)
        if receipt["source_url"] != url or receipt["sha256"] != sha or (expected_md5 and md5 != expected_md5):
            raise ValueError("Existing file differs from its recorded source/checksum: " + str(path))
        print("Reuse: " + path.name, flush=True)
        return receipt
    part = path.with_name(path.name + ".part")
    headers_path = path.with_name(path.name + ".headers")
    print("Fetch: " + path.name, flush=True)
    subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
        "--proto-redir", "=https", "--max-redirs", "3", "--connect-timeout", "20",
        "--max-time", "900", "--speed-time", "90", "--speed-limit", "1024",
        "--retry", "3", "--retry-delay", "5", "--limit-rate", str(int(limit * 1024 * 1024)),
        "--continue-at", "-", "--dump-header", str(headers_path), "--output", str(part), url,
    ], check=True)
    sha, md5 = hashes(part)
    if expected_md5 and md5 != expected_md5:
        raise ValueError("NCBI MD5 mismatch; partial file retained: " + str(part))
    headers = {}
    for line in headers_path.read_text().splitlines():
        if line.startswith("HTTP/"):
            headers = {}
        elif ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    receipt = dict(source_url=url, retrieved_at=utc_now(), bytes=part.stat().st_size,
                   sha256=sha, md5=md5, upstream_last_modified=headers.get("last-modified"),
                   upstream_etag=headers.get("etag"))
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    part.replace(path)
    return receipt


def stage(root, limit, with_fasta=False):
    entries = []
    staging = root / ".staging" / "refseq-pinned-20261028"
    for bucket, assembly, accession, release in RELEASES:
        relative_dir = Path(bucket) / "refseq" / release
        work = staging / release
        work.mkdir(parents=True, exist_ok=True)
        base = "https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9606/" + release + "/"
        prefix = accession + "_" + assembly
        filenames = ["md5checksums.txt", "README_" + release, prefix + "_assembly_report.txt",
                     release + "_annotation_report.xml", prefix + "_genomic.gff.gz"]
        if with_fasta:
            filenames.append(prefix + "_genomic.fna.gz")
        checksums = {}
        for name in filenames:
            expected = checksums.get(name)
            if name != "md5checksums.txt" and expected is None:
                raise ValueError("File missing from official checksum list: " + name)
            path = work / name
            receipt = fetch(base + name, path, limit, expected)
            if name == "md5checksums.txt":
                for line in path.read_text().splitlines():
                    checksum, filename = line.split(maxsplit=1)
                    checksums[filename.removeprefix("./")] = checksum
            extra = {"license_url": LICENSE_URL, "upstream_last_modified": receipt["upstream_last_modified"],
                     "upstream_etag": receipt["upstream_etag"], "source_checked_at": CHECKED_AT,
                     "upstream_checksum_url": base + "md5checksums.txt",
                     "assembly_bucket": bucket, "assembly_alias_note": "Directory alias only; exact assembly accession is authoritative."}
            if name.endswith(".gff.gz"):
                directives = []
                with gzip.open(path, "rt") as handle:
                    for line in handle:
                        if not line.startswith("#"):
                            break
                        if line.startswith("#!"):
                            directives.append(line.rstrip())
                    # Read to EOF to verify gzip CRC without unpacking a second copy.
                    while handle.read(1024 * 1024):
                        pass
                if not any(accession in line for line in directives):
                    raise ValueError("GFF assembly header mismatch: " + name)
                extra["gff_directives"] = directives
                extra["gzip_crc_verified"] = True
            if name.endswith("_genomic.fna.gz"):
                with gzip.open(path, "rb") as handle:
                    first_header = handle.readline().decode("ascii").strip()
                    if not first_header.startswith(">"):
                        raise ValueError("Not a genomic FASTA: " + name)
                    while handle.read(1024 * 1024):
                        pass
                extra.update(gzip_crc_verified=True, first_fasta_header=first_header,
                             sequence_set="NCBI full assembly; use assembly_report.txt to select sequence roles",
                             pipeline_note="Not interchangeable with UCSC hg19/hg38 or b37/hs37d5 by directory alias alone")
            entries.append(dict(source_path=str(path), relative_path=str(relative_dir / name),
                                provider="NCBI RefSeq", dataset="human-genome-annotation", assembly=assembly,
                                assembly_accession=accession, annotation_release=release, source_url=base + name,
                                retrieved_at=receipt["retrieved_at"], upstream_md5=expected, extra=extra))
    plan = staging / "import-plan.json"
    plan.write_text(json.dumps({"entries": entries}, indent=2) + "\n")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/appdata/02_resource/databases"))
    parser.add_argument("--limit-mib-s", type=float, default=2)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--with-fasta", action="store_true", help="Also fetch matching complete genomic FASTA assemblies")
    args = parser.parse_args()
    if args.limit_mib_s <= 0:
        parser.error("--limit-mib-s must be positive")
    args.root.mkdir(parents=True, exist_ok=True)
    with (args.root / ".refseq-fetch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = stage(args.root, args.limit_mib_s, args.with_fasta)
        print("Import plan: " + str(plan), flush=True)
        if not args.stage_only:
            subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("manage_reference_files.py")),
                            "import", "--root", str(args.root), "--plan", str(plan)], check=True)


if __name__ == "__main__":
    main()
