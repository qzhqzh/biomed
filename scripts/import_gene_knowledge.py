#!/usr/bin/env python3
"""Download NCBI gene_info + MANE summary and build local knowledge files.

Outputs under data/knowledge/:
  - meta.json              source versions / counts / MANE notes
  - genes.jsonl            one gene per line (slim fields)
  - transcript-map.json    ENST ↔ NM (MANE Select / Plus Clinical)

Usage:
  python scripts/import_gene_knowledge.py
  python scripts/import_gene_knowledge.py --skip-download
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "knowledge"
RAW_DIR = OUT_DIR / "raw"

GENE_INFO_URL = (
    "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/"
    "Homo_sapiens.gene_info.gz"
)
MANE_CURRENT = "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/"
MANE_SUMMARY_URL = MANE_CURRENT + "MANE.GRCh38.v1.5.summary.txt.gz"
MANE_README_URL = MANE_CURRENT + "README_versions.txt"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    print(f"Downloading {url}")
    print(f"  -> {dest}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            expected = resp.headers.get("Content-Length")
            expected_n = int(expected) if expected and expected.isdigit() else None
            written = 0
            with tmp.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
            if expected_n is not None and written != expected_n:
                raise RuntimeError(
                    f"incomplete download: got {written} bytes, expected {expected_n}"
                )
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    # Validate gzip if applicable
    if dest.suffix == ".gz" or dest.name.endswith(".gz"):
        with gzip.open(dest, "rb") as fh:
            while fh.read(1024 * 64):
                pass
    print(f"  done ({dest.stat().st_size:,} bytes)")


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_mane_readme(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        k, v = line.split("\t", 1)
        meta[k.strip()] = v.strip()
    return meta


def strip_version(acc: str) -> str:
    value = acc.split(".", 1)[0].strip()
    return "" if value == "-" else value


def parse_gene_info(path: Path, out_jsonl: Path) -> dict:
    count = 0
    by_type: dict[str, int] = {}
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh, out_jsonl.open(
        "w", encoding="utf-8"
    ) as out:
        header = None
        for line in fh:
            if not line.strip():
                continue
            if line.startswith("#"):
                header = line.lstrip("#").rstrip("\n").split("\t")
                continue
            if header is None:
                continue
            cols = line.rstrip("\n").split("\t")
            row = dict(zip(header, cols))
            gene_type = row.get("type_of_gene") or "unknown"
            by_type[gene_type] = by_type.get(gene_type, 0) + 1

            xrefs = row.get("dbXrefs") or ""
            ensembl = None
            hgnc = None
            for part in xrefs.split("|"):
                if part.startswith("Ensembl:"):
                    ensembl = part.split(":", 1)[1]
                elif part.startswith("HGNC:HGNC:"):
                    hgnc = part.split(":")[-1]
                elif part.startswith("HGNC:"):
                    hgnc = part.split(":", 1)[1]

            synonyms = [
                s for s in (row.get("Synonyms") or "").split("|") if s and s != "-"
            ]
            rec = {
                "gene_id": int(row["GeneID"]),
                "symbol": row.get("Symbol") or "",
                "name": row.get("description")
                or row.get("Full_name_from_nomenclature_authority")
                or "",
                "type": gene_type,
                "chromosome": row.get("chromosome")
                if row.get("chromosome") != "-"
                else None,
                "map_location": row.get("map_location")
                if row.get("map_location") != "-"
                else None,
                "ensembl_gene": ensembl,
                "hgnc_id": hgnc,
                "synonyms": synonyms,
            }
            out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1

    return {"gene_count": count, "by_type": dict(sorted(by_type.items()))}


def parse_mane_summary(path: Path) -> tuple[dict, dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    transcript_map: dict[str, dict] = {}
    select = 0
    plus = 0

    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        header = None
        for line in fh:
            if not line.strip():
                continue
            if line.startswith("#"):
                header = line.lstrip("#").rstrip("\n").split("\t")
                continue
            if header is None:
                continue
            cols = line.rstrip("\n").split("\t")
            row = dict(zip(header, cols))
            status = (row.get("MANE_status") or "").strip()
            gene = (row.get("symbol") or "").strip()
            nm = strip_version(row.get("RefSeq_nuc") or "")
            np = strip_version(row.get("RefSeq_prot") or "")
            enst = strip_version(row.get("Ensembl_nuc") or "")
            ensp = strip_version(row.get("Ensembl_prot") or "")
            gene_id = (row.get("NCBI_GeneID") or "").replace("GeneID:", "")
            mane_flag = "select" if "Select" in status else "plus_clinical"

            if "Select" in status:
                select += 1
            else:
                plus += 1

            note = f"MANE {status}"
            if enst:
                transcript_map[enst] = {
                    "gene": gene,
                    "gene_id": gene_id or None,
                    "refseq": [nm] if nm else [],
                    "protein_ensembl": [ensp] if ensp else [],
                    "protein_refseq": [np] if np else [],
                    "mane": True,
                    "mane_status": mane_flag,
                    "note": note,
                }
            if nm:
                transcript_map[nm] = {
                    "gene": gene,
                    "gene_id": gene_id or None,
                    "ensembl": [enst] if enst else [],
                    "protein_ensembl": [ensp] if ensp else [],
                    "protein_refseq": [np] if np else [],
                    "mane": True,
                    "mane_status": mane_flag,
                    "note": note,
                }
            if ensp and ensp not in transcript_map:
                transcript_map[ensp] = {
                    "gene": gene,
                    "refseq": [np] if np else [],
                    "mane": True,
                    "mane_status": mane_flag,
                }
            if np and np not in transcript_map:
                transcript_map[np] = {
                    "gene": gene,
                    "ensembl": [ensp] if ensp else [],
                    "mane": True,
                    "mane_status": mane_flag,
                }

    seeds = {
        "NM_001126115": {
            "gene": "TP53",
            "ensembl": ["ENST00000619186"],
            "mane": False,
            "mane_status": None,
            "note": "RefSeq transcript variant 5; not MANE Select",
        },
        "ENST00000619186": {
            "gene": "TP53",
            "refseq": ["NM_001126115"],
            "mane": False,
            "mane_status": None,
            "note": "Ensembl match for NM_001126115; not MANE Select",
        },
    }
    for k, v in seeds.items():
        transcript_map.setdefault(k, v)

    stats = {
        "mane_select": select,
        "mane_plus_clinical": plus,
        "accession_keys": len(transcript_map),
    }
    return transcript_map, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--gene-info-url", default=GENE_INFO_URL)
    parser.add_argument("--mane-url", default=MANE_SUMMARY_URL)
    parser.add_argument("--force-mane", action="store_true", help="Re-download MANE even if present")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    gene_info_path = RAW_DIR / "Homo_sapiens.gene_info.gz"
    mane_path = RAW_DIR / "MANE.summary.txt.gz"
    mane_readme_path = RAW_DIR / "README_versions.txt"

    mane_release: dict[str, str] = {}
    if not args.skip_download:
        if not gene_info_path.is_file():
            download(args.gene_info_url, gene_info_path)
        else:
            print(f"Keeping existing {gene_info_path}")
        if args.force_mane or not mane_path.is_file():
            download(args.mane_url, mane_path)
        else:
            # Validate existing gzip; re-download if corrupt
            try:
                with gzip.open(mane_path, "rb") as fh:
                    fh.read(64)
            except OSError:
                print("Existing MANE archive corrupt; re-downloading")
                download(args.mane_url, mane_path)
        try:
            readme = fetch_text(MANE_README_URL)
            mane_readme_path.write_text(readme, encoding="utf-8")
            mane_release = parse_mane_readme(readme)
            print(f"MANE release: {mane_release}")
        except Exception as exc:
            print(f"Warning: could not fetch MANE README_versions.txt: {exc}")
    else:
        if not gene_info_path.is_file() or not mane_path.is_file():
            print("Missing raw downloads; run without --skip-download", file=sys.stderr)
            return 1
        if mane_readme_path.is_file():
            mane_release = parse_mane_readme(mane_readme_path.read_text(encoding="utf-8"))

    # Always force MANE re-download if gzip invalid
    try:
        with gzip.open(mane_path, "rb") as fh:
            while fh.read(1024 * 256):
                pass
    except OSError:
        print("MANE gzip invalid; re-downloading")
        download(args.mane_url, mane_path)

    genes_out = OUT_DIR / "genes.jsonl"
    print(f"Parsing gene_info -> {genes_out}")
    gene_stats = parse_gene_info(gene_info_path, genes_out)
    print(f"  genes: {gene_stats['gene_count']:,}")

    print("Parsing MANE summary -> transcript-map.json")
    tmap, mane_stats = parse_mane_summary(mane_path)
    mane_version = mane_release.get("MANE Version") or "1.5"
    tmap_out = OUT_DIR / "transcript-map.json"
    payload = {
        "_comment": (
            "ENST/NM crosswalk from NCBI MANE (+ small isoform seeds). "
            "Keys without version. MANE is defined on GRCh38 only."
        ),
        "meta": {
            "source": "NCBI MANE summary",
            "url": args.mane_url,
            "ftp_current": MANE_CURRENT,
            "assembly": "GRCh38",
            "assembly_note": (
                "MANE is only produced for GRCh38; there is no official MANE for GRCh37/hg19."
            ),
            "mane_version": mane_version,
            "refseq_annotation": mane_release.get("NCBI RefSeq Annotation Release"),
            "ensembl_release": mane_release.get("Ensembl Release"),
            "sets": ["MANE Select", "MANE Plus Clinical"],
            "imported_at": datetime.now(timezone.utc).isoformat(),
            **mane_stats,
        },
        "records": tmap,
    }
    tmap_out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"  MANE v{mane_version} Select: {mane_stats['mane_select']:,}")
    print(f"  accession keys: {mane_stats['accession_keys']:,}")

    meta = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "mane": {
            "version": mane_version,
            "assembly": "GRCh38",
            "assembly_note": (
                "仅针对 GRCh38；无官方 GRCh37/hg19 MANE。"
                "集合含 MANE Select（每蛋白编码基因一条常用转录本）"
                "与 MANE Plus Clinical（临床额外转录本）。"
            ),
            "refseq_annotation": mane_release.get("NCBI RefSeq Annotation Release"),
            "ensembl_release": mane_release.get("Ensembl Release"),
            "ftp_current": MANE_CURRENT,
            "summary_url": args.mane_url,
            "docs": {
                "ncbi": "https://www.ncbi.nlm.nih.gov/refseq/MANE/",
                "ensembl": "https://www.ensembl.org/info/genome/genebuild/mane.html",
            },
            **mane_stats,
        },
        "sources": {
            "gene_info": {
                "url": args.gene_info_url,
                "file": str(gene_info_path.relative_to(ROOT)),
            },
            "mane": {
                "url": args.mane_url,
                "file": str(mane_path.relative_to(ROOT)),
                "readme": str(mane_readme_path.relative_to(ROOT))
                if mane_readme_path.is_file()
                else None,
            },
        },
        "genes": gene_stats,
        "transcripts": mane_stats,
        "files": {
            "genes": "genes.jsonl",
            "transcript_map": "transcript-map.json",
        },
    }
    (OUT_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Wrote meta.json")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
