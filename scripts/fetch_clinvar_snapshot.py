#!/usr/bin/env python3
"""Fetch and register the dated ClinVar VCF snapshot 2026-10-19.

This is a pinned dated archive, not a monthly release. Standard library plus
curl; network requests share RefSeq's lock and rate limit. Existing checked
downloads are reused. No running database, pipeline or service is changed.
"""
import argparse
from datetime import datetime
import fcntl
import gzip
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from fetch_refseq_annotations import fetch, hashes


SNAPSHOT = "20261019"
CHECKED_AT = "2026-10-28"
RELEASES = (
    ("hg19", "GRCh37", "5411a54d118a5b56705cfed044d14411"),
    ("hg38", "GRCh38", "e649bbb1dfe11baafa9feab81a7a7cdd"),
)
LICENSE_URL = "https://www.ncbi.nlm.nih.gov/home/about/policies/"


def checksum_for(path, filename):
    """NCBI's .md5 records an internal absolute path; match its basename only."""
    matches = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        checksum, remote_path = line.split(maxsplit=1)
        if PurePosixPath(remote_path.lstrip("*")).name == filename:
            if not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
                raise ValueError("Invalid upstream MD5: " + str(path))
            matches.append(checksum.lower())
    if len(matches) != 1:
        raise ValueError("Expected exactly one upstream checksum for " + filename)
    return matches[0]


def validate_vcf(path, assembly):
    headers = {}
    with gzip.open(path, "rb") as handle:
        for raw in handle:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("##"):
                key, separator, value = line[2:].partition("=")
                if separator and key in ("fileformat", "fileDate", "source", "reference", "assembly", "assembly_accession"):
                    if key in headers and headers[key] != value:
                        raise ValueError("Conflicting VCF header: " + key)
                    headers[key] = value
                continue
            if line.split("\t")[:8] != ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]:
                raise ValueError("Missing or invalid VCF column header: " + str(path))
            break
        else:
            raise ValueError("VCF has no column header: " + str(path))
        while handle.read(1024 * 1024):
            pass  # Consume every gzip member to verify CRC without another copy.
    file_date = headers.get("fileDate", "").replace("-", "")
    if file_date != SNAPSHOT or headers.get("reference") != assembly or headers.get("source") != "ClinVar":
        raise ValueError("VCF date/source/assembly differs from pinned snapshot: " + str(headers))
    datetime.strptime(file_date, "%Y%m%d")
    if not headers.get("fileformat", "").startswith("VCFv4."):
        raise ValueError("Unexpected VCF format: " + str(headers))
    accession_text = " ".join(headers.get(key, "") for key in ("reference", "assembly", "assembly_accession"))
    accessions = set(re.findall(r"GC[AF]_\d+\.\d+", accession_text))
    if len(accessions) > 1:
        raise ValueError("Ambiguous assembly accession in VCF header")
    return headers, next(iter(accessions), "not-specified-in-vcf")


def validate_tbi(path):
    with gzip.open(path, "rb") as handle:
        if handle.read(4) != b"TBI\x01":
            raise ValueError("Not a Tabix index: " + str(path))
        while handle.read(1024 * 1024):
            pass


def stage(root, limit):
    staging = root / ".staging" / ("clinvar-" + SNAPSHOT)
    entries = []
    for bucket, assembly, pinned_md5 in RELEASES:
        work = staging / assembly
        work.mkdir(parents=True, exist_ok=True)
        relative_dir = Path(bucket) / "clinvar" / SNAPSHOT
        base = f"https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_{assembly}/archive_2.0/2026/"
        vcf_name = "clinvar_" + SNAPSHOT + ".vcf.gz"
        checksum_name = vcf_name + ".md5"
        checksum_receipt = fetch(base + checksum_name, work / checksum_name, limit)
        expected_md5 = checksum_for(work / checksum_name, vcf_name)
        if expected_md5 != pinned_md5:
            raise ValueError("Upstream checksum changed for the pinned ClinVar snapshot: " + assembly)
        vcf_receipt = fetch(base + vcf_name, work / vcf_name, limit, expected_md5)
        vcf_headers, accession = validate_vcf(work / vcf_name, assembly)
        tbi_name = vcf_name + ".tbi"
        tbi_receipt = fetch(base + tbi_name, work / tbi_name, limit)
        validate_tbi(work / tbi_name)
        downloads = [(checksum_name, checksum_receipt, None),
                     (vcf_name, vcf_receipt, expected_md5), (tbi_name, tbi_receipt, None)]
        error_name = "clinvar_" + SNAPSHOT + ".vcf.error.txt"
        try:
            error_receipt = fetch(base + error_name, work / error_name, limit)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 22:
                raise
            headers_path = work / (error_name + ".headers")
            statuses = [line.split()[1] for line in headers_path.read_text().splitlines() if line.startswith("HTTP/")]
            if not statuses or statuses[-1] != "404":
                raise
            print("Optional upstream error report unavailable (HTTP 404): " + error_name, flush=True)
        else:
            downloads.append((error_name, error_receipt, None))
        for name, receipt, md5 in downloads:
            path = work / name
            if hashes(path)[0] != receipt["sha256"]:
                raise ValueError("File changed during validation: " + str(path))
            extra = {
                "snapshot_kind": "dated_archive", "snapshot_date": "2026-10-19",
                "license_url": LICENSE_URL, "source_checked_at": CHECKED_AT,
                "assembly_bucket": bucket, "vcf_header": vcf_headers,
                "assembly_alias_note": "Directory alias only; VCF declares GRCh37/GRCh38 without a patch-level accession.",
                "upstream_last_modified": receipt["upstream_last_modified"],
                "upstream_etag": receipt["upstream_etag"],
            }
            if name == vcf_name:
                extra.update(gzip_crc_verified=True, upstream_checksum_url=base + checksum_name)
            elif name == tbi_name:
                extra.update(gzip_crc_verified=True, tabix_magic_verified=True,
                             upstream_checksum_status="not_published_HTTP_404_at_" + CHECKED_AT,
                             checked_checksum_url=base + tbi_name + ".md5",
                             integrity_note="Local SHA256 and gzip CRC verified; no upstream MD5 supplied.")
            elif name == error_name:
                extra["file_role"] = "upstream_vcf_issue_report"
            entries.append(dict(source_path=str(path), relative_path=str(relative_dir / name),
                                provider="NCBI ClinVar", dataset="clinvar-vcf", assembly=assembly,
                                assembly_accession=accession, annotation_release=SNAPSHOT,
                                source_url=base + name, retrieved_at=receipt["retrieved_at"],
                                upstream_md5=md5, extra=extra))
    readme_name = "README_VCF.txt"
    readme_url = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/" + readme_name
    readme_receipt = fetch(readme_url, staging / readme_name, limit)
    retrieved_date = readme_receipt["retrieved_at"][:10]
    entries.append(dict(source_path=str(staging / readme_name),
                        relative_path=str(Path("shared/clinvar/docs") / ("retrieved-" + retrieved_date) / readme_name),
                        provider="NCBI ClinVar", dataset="clinvar-vcf-documentation", assembly="not-assembly-specific",
                        assembly_accession="not-assembly-specific", annotation_release=None,
                        source_url=readme_url, retrieved_at=readme_receipt["retrieved_at"], upstream_md5=None,
                        extra={"snapshot_kind": "documentation_retrieval_snapshot",
                               "snapshot_date": retrieved_date, "license_url": LICENSE_URL,
                               "version_note": "General VCF documentation retrieved on this date; not a document specific to the 2026-10-19 archive.",
                               "upstream_last_modified": readme_receipt["upstream_last_modified"],
                               "upstream_etag": readme_receipt["upstream_etag"]}))
    plan = staging / "import-plan.json"
    plan.write_text(json.dumps({"entries": entries}, indent=2) + "\n")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/appdata/02_resource/databases"))
    parser.add_argument("--limit-mib-s", type=float, default=2)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.limit_mib_s) or args.limit_mib_s <= 0:
        parser.error("--limit-mib-s must be finite and positive")
    args.root.mkdir(parents=True, exist_ok=True)
    print("Waiting for shared reference-download lock...", flush=True)
    with (args.root / ".refseq-fetch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = stage(args.root, args.limit_mib_s)
        print("Import plan: " + str(plan), flush=True)
        if not args.stage_only:
            subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("manage_reference_files.py")),
                            "import", "--root", str(args.root), "--plan", str(plan)], check=True)


if __name__ == "__main__":
    main()
