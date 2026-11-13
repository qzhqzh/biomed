"""Versioned transcript structures, separate from the literature database.

RefGene exonCount is a count of genomic alignment blocks. Only validated exon
features on a complete RefSeq RNA record populate the biological exon_count.
Each import creates an immutable SQLite release and atomically changes a pointer.
"""

from __future__ import annotations

import csv
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

from django.conf import settings

REFSEQ = re.compile(r"^(?:NM|NR)_\d+\.\d+$")
ENSEMBL = re.compile(r"^ENST\d+\.\d+$")
ACCESSION_QUERY = re.compile(r"^(?:(?:NM|NR)_\d+|ENST\d+)(?:\.\d+)?$")
SCHEMA_VERSION = 1


class StructureImportError(ValueError):
    """Input cannot safely be published as a transcript structure release."""


def structure_root(root=None) -> Path:
    if root is not None:
        return Path(root)
    return Path(getattr(
        settings, "TRANSCRIPT_STRUCTURE_DIR",
        Path(settings.CURATION_STORAGE_DIR) / "reference" / "transcript-structures",
    ))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _text_file(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig") as handle:
        yield handle


def _source(key, path, metadata):
    supplied = metadata.get(key, {})
    if not isinstance(supplied, dict):
        raise StructureImportError(f"Invalid source metadata: {key}")
    checksum = _sha256(path)
    if supplied.get("sha256") and supplied["sha256"] != checksum:
        raise StructureImportError(f"Source metadata SHA-256 mismatch: {key}")
    if supplied.get("bytes") is not None and supplied["bytes"] != Path(path).stat().st_size:
        raise StructureImportError(f"Source metadata size mismatch: {key}")
    time_basis = supplied.get("time_basis") or "local_file_observed_at_import"
    retrieved_at = supplied.get("retrieved_at")
    if retrieved_at is None and time_basis in (
        "http_response_received_at", "http_fetch_completed_at", "download_completed_at", "response_fully_received_at",
    ):
        retrieved_at = supplied.get("observed_at")
    return {
        "id": f"{key}:{checksum[:16]}",
        "name": supplied.get("name") or key,
        "url": supplied.get("url"),
        "sha256": checksum,
        "release": supplied.get("release"),
        "observed_at": supplied.get("observed_at") or _now(),
        "retrieved_at": retrieved_at,
        "time_basis": time_basis,
        "imported_at": _now(),
        "original_path": supplied.get("original_path") or supplied.get("path") or str(Path(path).resolve()),
        "import_path": str(Path(path).resolve()),
        "expected_accession": supplied.get("accession"),
        "expected_accessions": supplied.get("accessions"),
        "expected_gene": supplied.get("gene"),
    }


def _schema(connection):
    connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE transcripts (
            accession TEXT PRIMARY KEY,
            base_accession TEXT NOT NULL,
            gene TEXT NOT NULL DEFAULT '',
            gene_authority INTEGER NOT NULL DEFAULT 0,
            gene_aliases TEXT NOT NULL DEFAULT '[]',
            gene_id TEXT NOT NULL DEFAULT '',
            mane_status TEXT NOT NULL DEFAULT '',
            exon_count INTEGER,
            exon_status TEXT NOT NULL DEFAULT 'unverified',
            exon_source_id TEXT REFERENCES sources(id),
            exons TEXT NOT NULL DEFAULT '[]',
            exon_support TEXT NOT NULL DEFAULT 'null',
            verification_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX transcripts_base ON transcripts(base_accession);
        CREATE INDEX transcripts_gene ON transcripts(gene COLLATE NOCASE);
        CREATE INDEX transcripts_gene_id ON transcripts(gene_id);
        CREATE TABLE gene_aliases (
            accession TEXT NOT NULL REFERENCES transcripts(accession),
            alias TEXT NOT NULL,
            PRIMARY KEY (accession, alias)
        );
        CREATE INDEX gene_aliases_symbol ON gene_aliases(alias COLLATE NOCASE);
        CREATE TABLE mappings (
            id INTEGER PRIMARY KEY,
            accession TEXT NOT NULL REFERENCES transcripts(accession),
            assembly TEXT NOT NULL,
            chrom TEXT NOT NULL,
            gene TEXT NOT NULL,
            strand TEXT NOT NULL,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            raw_exon_count INTEGER NOT NULL,
            source_line INTEGER NOT NULL,
            source_row_format TEXT NOT NULL,
            exon_starts TEXT NOT NULL,
            exon_ends TEXT NOT NULL,
            source_id TEXT NOT NULL REFERENCES sources(id)
        );
        CREATE INDEX mappings_accession ON mappings(accession);
        CREATE TABLE aliases (
            alias TEXT NOT NULL,
            base_alias TEXT NOT NULL,
            accession TEXT NOT NULL REFERENCES transcripts(accession),
            source_id TEXT NOT NULL REFERENCES sources(id),
            PRIMARY KEY (alias, accession)
        );
        CREATE INDEX aliases_base ON aliases(base_alias);
    """)


def _ensure_transcript(connection, accession, gene="", authority=0, gene_id="", synonyms=()):
    if not REFSEQ.fullmatch(accession):
        raise StructureImportError(f"Expected versioned NM_/NR_ transcript: {accession}")
    connection.execute(
        "INSERT OR IGNORE INTO transcripts(accession,base_accession,gene) VALUES(?,?,?)",
        (accession, accession.split(".")[0], gene),
    )
    if gene:
        existing, previous_authority, existing_gene_id, aliases_json = connection.execute(
            "SELECT gene,gene_authority,gene_id,gene_aliases FROM transcripts WHERE accession=?", (accession,),
        ).fetchone()
        if gene_id and existing_gene_id and gene_id != existing_gene_id:
            raise StructureImportError(f"Conflicting GeneID for {accession}: {existing_gene_id}, {gene_id}")
        if previous_authority and existing and existing != gene:
            # Official symbols can change between MANE and RefSeq snapshots.
            # Allow only an explicit synonym AND matching stable GeneID; retain
            # the existing canonical symbol and expose the official aliases.
            if not (gene_id and gene_id == existing_gene_id and existing in synonyms):
                raise StructureImportError(f"Conflicting gene identity for {accession}: {existing}, {gene}")
            synonyms = (*synonyms, gene)
            gene = existing
        if authority or not existing:
            aliases = sorted(set(json.loads(aliases_json)) | (set(synonyms) - {gene}))
            connection.execute(
                "UPDATE transcripts SET gene=?,gene_authority=?,gene_id=?,gene_aliases=? WHERE accession=?",
                (gene, authority, gene_id or existing_gene_id, _json(aliases), accession),
            )
            connection.executemany("INSERT OR IGNORE INTO gene_aliases(accession,alias) VALUES(?,?)",
                                   ((accession, alias) for alias in aliases))


def _import_refgene(connection, path, assembly, source):
    rows = 0
    with _text_file(path) as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\r\n").split("\t")
            try:
                row_format = "genePredExt_with_bin"
                if (
                    len(fields) == 15 and fields[12] in ("none", "unk", "incmpl", "cmpl")
                    and fields[13] in ("none", "unk", "incmpl", "cmpl")
                    and fields[11] and not fields[11].isdigit()
                    and fields[11] not in ("none", "unk", "incmpl", "cmpl")
                ):
                    # A legacy local row omits only score. Recognize its complete
                    # gene/status/frame layout instead of shifting gene to cmpl.
                    frames = [int(value) for value in fields[14].rstrip(",").split(",")]
                    if len(frames) != int(fields[8]) or any(frame not in (-1, 0, 1, 2) for frame in frames):
                        raise ValueError("invalid score-less genePredExt frame layout")
                    fields.insert(11, "")
                    row_format = "genePredExt_with_bin_missing_score"
                    source.setdefault("format_warnings", []).append(
                        {"line": line_number, "accession": fields[1], "kind": "missing_score_column"},
                    )
                if len(fields) != 16:
                    raise ValueError("expected 16 genePredExt columns including bin")
                accession, chrom, strand = fields[1:4]
                start, end, count = int(fields[4]), int(fields[5]), int(fields[8])
                starts = [int(value) for value in fields[9].rstrip(",").split(",")]
                ends = [int(value) for value in fields[10].rstrip(",").split(",")]
                if strand not in ("+", "-") or not chrom or start < 0 or start >= end:
                    raise ValueError("invalid transcript range or strand")
                if count < 1 or len(starts) != count or len(ends) != count:
                    raise ValueError("exonCount does not match coordinate arrays")
                if any(not start <= left < right <= end for left, right in zip(starts, ends)):
                    raise ValueError("exon outside transcript range")
                if any(starts[index] < ends[index - 1] for index in range(1, count)):
                    raise ValueError("overlapping or unordered genomic blocks")
                if starts[0] != start or ends[-1] != end:
                    raise ValueError("transcript endpoints do not match block endpoints")
                _ensure_transcript(connection, accession, fields[12])
            except (ValueError, IndexError) as exc:
                raise StructureImportError(f"{path}:{line_number}: {exc}") from exc
            connection.execute(
                "INSERT INTO mappings(accession,assembly,chrom,gene,strand,start,end,raw_exon_count,source_line,source_row_format,"
                "exon_starts,exon_ends,source_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (accession, assembly, chrom, fields[12], strand, start, end, count,
                 line_number, row_format, _json(starts), _json(ends), source["id"]),
            )
            rows += 1
    if not rows:
        raise StructureImportError(f"No transcript mappings in {path}")
    return rows


def _import_mane(connection, path, source):
    rows = 0
    with _text_file(path) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not {"RefSeq_nuc", "Ensembl_nuc", "symbol", "MANE_status"}.issubset(reader.fieldnames or []):
            raise StructureImportError("MANE summary missing transcript columns")
        for row in reader:
            refseq, ensembl = row["RefSeq_nuc"].strip(), row["Ensembl_nuc"].strip()
            if not REFSEQ.fullmatch(refseq) or not ENSEMBL.fullmatch(ensembl):
                raise StructureImportError(f"Invalid MANE transcript pair: {refseq}, {ensembl}")
            gene_id = row.get("#NCBI_GeneID", row.get("NCBI_GeneID", "")).removeprefix("GeneID:")
            _ensure_transcript(connection, refseq, row["symbol"], authority=1, gene_id=gene_id)
            current = connection.execute(
                "SELECT gene_id,mane_status FROM transcripts WHERE accession=?", (refseq,),
            ).fetchone()
            if current[0] and current[0] != gene_id:
                raise StructureImportError(f"Conflicting MANE GeneID for {refseq}")
            statuses = sorted(set(filter(None, [*current[1].split("; "), row["MANE_status"]])))
            connection.execute(
                "UPDATE transcripts SET gene_id=?,mane_status=? WHERE accession=?",
                (gene_id, "; ".join(statuses), refseq),
            )
            connection.execute(
                "INSERT OR IGNORE INTO aliases(alias,base_alias,accession,source_id) VALUES(?,?,?,?)",
                (ensembl, ensembl.split(".")[0], refseq, source["id"]),
            )
            rows += 1
    if not rows:
        raise StructureImportError("MANE summary contains no transcript pairs")
    return rows


def _genbank_records(text):
    """Parse complete RNA records; unsupported exon locations stay unverified."""
    parts = re.split(r"(?m)^//\s*$", text)
    if parts[-1].strip():
        raise StructureImportError("Truncated GenBank response (missing // terminator)")
    records = []
    for block in parts[:-1]:
        if not block.strip():
            continue
        locus = re.search(r"(?m)^LOCUS\s+\S+\s+(\d+)\s+bp\s+(\S+)", block)
        version = re.search(r"(?m)^VERSION\s+(\S+)", block)
        accession_line = re.search(r"(?m)^ACCESSION\s+(\S+)", block)
        organism = re.search(r"(?m)^\s+ORGANISM\s+(.+)$", block)
        if not locus or locus[2] not in ("mRNA", "RNA", "ncRNA"):
            raise StructureImportError("GenBank record is not a complete RNA accession")
        if not version or not REFSEQ.fullmatch(version[1]):
            raise StructureImportError("GenBank VERSION must identify an NM_/NR_ transcript version")
        accession, length = version[1], int(locus[1])
        if not accession_line or accession_line[1] != accession.split(".")[0]:
            raise StructureImportError(f"GenBank ACCESSION/VERSION mismatch: {accession}")
        if not organism or organism[1].strip() != "Homo sapiens":
            raise StructureImportError(f"Non-human or missing GenBank organism: {accession}")
        feature_match = re.search(r"(?ms)^FEATURES\s+.*?\n(.*?)(?=^ORIGIN\b)", block)
        if not feature_match:
            raise StructureImportError(f"Missing GenBank FEATURES or ORIGIN: {accession}")
        features = []
        for line in feature_match[1].splitlines():
            if len(line) < 21 or not line.startswith("     "):
                if line.strip():
                    raise StructureImportError(f"Malformed GenBank feature: {accession}")
                continue
            key, value = line[5:21].strip(), line[21:].strip()
            if key:
                features.append({"key": key, "location": value, "qualifiers": []})
            elif features and value.startswith("/"):
                features[-1]["qualifiers"].append(value)
            elif features and features[-1]["qualifiers"]:
                features[-1]["qualifiers"][-1] += value
            elif features:
                features[-1]["location"] += value
        sources = [feature for feature in features if feature["key"] == "source"]
        if len(sources) != 1 or '/organism="Homo sapiens"' not in sources[0]["qualifiers"]:
            raise StructureImportError(f"Missing or conflicting GenBank source organism: {accession}")
        if sources[0]["location"] != f"1..{length}":
            raise StructureImportError(f"Incomplete GenBank source range: {accession}")
        genes, gene_ids, gene_synonyms = set(), set(), set()
        for feature in features:
            for qualifier in feature["qualifiers"]:
                match = re.fullmatch(r'/gene="([^"]+)"', qualifier)
                if match:
                    genes.add(match[1])
                match = re.fullmatch(r'/db_xref="GeneID:(\d+)"', qualifier)
                if match:
                    gene_ids.add(match[1])
                match = re.fullmatch(r'/gene_synonym="([^"]+)"', qualifier)
                if match:
                    gene_synonyms.update(value.strip() for value in match[1].split(";") if value.strip())
        if len(genes) != 1:
            raise StructureImportError(f"Missing or ambiguous GenBank gene identity: {accession}")
        if len(gene_ids) > 1:
            raise StructureImportError(f"Conflicting GenBank GeneIDs: {accession}")
        gene = next(iter(genes))
        raw_exons = [feature for feature in features if feature["key"] == "exon"]
        exons, reason = [], ""
        for exon in raw_exons:
            match = re.fullmatch(r"(\d+)(?:\.\.(\d+))?", exon["location"])
            if not match:
                reason = "unsupported_or_fuzzy_exon_location"
                break
            start, end = int(match[1]), int(match[2] or match[1])
            if not 1 <= start <= end <= length:
                reason = "exon_outside_transcript"
                break
            exons.append({"start": start, "end": end})
        exons.sort(key=lambda item: (item["start"], item["end"]))
        if not reason and (
            not exons or exons[0]["start"] != 1 or exons[-1]["end"] != length
            or any(right["start"] != left["end"] + 1 for left, right in pairwise(exons))
        ):
            reason = "exon_features_do_not_cover_complete_transcript"
        sequence = re.split(r"(?m)^ORIGIN\b[^\n]*\n", block, maxsplit=1)[-1]
        bases = re.sub(r"[\s\d]", "", sequence).lower()
        if len(bases) != length or re.search(r"[^acgturyswkmbdhvn]", bases):
            raise StructureImportError(f"GenBank sequence does not match LOCUS length: {accession}")
        note = reason
        # RNA accessions can retain a post-transcriptional poly(A) tail which
        # has no genomic exon coordinates. Require an explicit exact polyA_site
        # in the same terminal A run; an arbitrary pure-A suffix alone is not
        # enough to infer that incomplete exon annotation is actually complete.
        if (
            reason == "exon_features_do_not_cover_complete_transcript"
            and exons and exons[0]["start"] == 1 and exons[-1]["end"] < length
            and all(right["start"] == left["end"] + 1 for left, right in pairwise(exons))
            and re.fullmatch(r"a+", bases[exons[-1]["end"]:])
        ):
            exon_end = exons[-1]["end"]
            supported_sites = [
                int(feature["location"]) for feature in features
                if feature["key"] == "polyA_site" and re.fullmatch(r"\d+", feature["location"])
                and 1 <= int(feature["location"]) <= length
                and re.fullmatch(r"a+", bases[min(int(feature["location"]), exon_end):])
            ]
            if supported_sites:
                reason = ""
                note = f"terminal_poly_a_tail_excluded:{length - exon_end}bp;polyA_site:{','.join(map(str, supported_sites))}"
        records.append({
            "accession": accession, "gene": gene,
            "gene_id": next(iter(gene_ids), ""), "gene_synonyms": sorted(gene_synonyms),
            "exons": exons if not reason else [],
            "exon_count": len(exons) if not reason else None,
            "exon_status": "unverified" if reason else "verified",
            "verification_note": note,
            "sequence_sha256": hashlib.sha256(bases.encode()).hexdigest(),
            "raw_exon_locations": [exon["location"] for exon in raw_exons],
            "replacement_accession": (
                match[1] if (match := re.search(
                    r"\[WARNING\].*?this sequence was replaced by\s+((?:NM|NR)_\d+\.\d+)\.",
                    block, re.DOTALL,
                )) else None
            ),
            # Transient input for cross-record validation; never stored in SQL.
            "_sequence": bases,
        })
    if not records:
        raise StructureImportError("GenBank response contains no records")
    return records


def resolve_supported_exon_counts(records):
    """Confirm an old version's own features using its declared replacement.

    This does not copy a newer version's count. It checks every internal exon
    sequence and both terminal splice anchors against an independently verified
    replacement, with the same nonempty GeneID and base accession. A reference
    promoted during this call cannot serve as evidence for another record.
    """
    references = {record["accession"]: record for record in records if record["exon_status"] == "verified"}
    resolved = []
    for original in records:
        record = dict(original)
        resolved.append(record)
        if record["exon_status"] == "verified":
            continue
        reference = references.get(record.get("replacement_accession"))
        if (
            reference is None or not record.get("gene_id")
            or record["gene_id"] != reference.get("gene_id")
            or record["accession"].split(".")[0] != reference["accession"].split(".")[0]
            or record.get("verification_note") != "exon_features_do_not_cover_complete_transcript"
        ):
            continue
        exons = []
        for location in record["raw_exon_locations"]:
            match = re.fullmatch(r"(\d+)(?:\.\.(\d+))?", location)
            if match is None:
                break
            exons.append({"start": int(match[1]), "end": int(match[2] or match[1])})
        else:
            exons.sort(key=lambda exon: (exon["start"], exon["end"]))
            sequence, reference_sequence = record.get("_sequence", ""), reference.get("_sequence", "")
            reference_exons = reference["exons"]
            if (
                len(exons) < 2 or len(exons) != len(reference_exons)
                or exons[0]["start"] != 1
                or any(not 1 <= exon["start"] <= exon["end"] <= len(sequence) for exon in exons)
                or any(right["start"] != left["end"] + 1 for left, right in pairwise(exons))
                or not re.fullmatch(r"a+", sequence[exons[-1]["end"]:])
                or not reference_sequence
            ):
                continue
            pieces = [sequence[exon["start"] - 1:exon["end"]] for exon in exons]
            reference_pieces = [reference_sequence[exon["start"] - 1:exon["end"]] for exon in reference_exons]
            if pieces[1:-1] != reference_pieces[1:-1]:
                continue
            first_anchor = min(100, len(pieces[0]), len(reference_pieces[0]))
            last_anchor = min(100, len(pieces[-1]), len(reference_pieces[-1]))
            if (
                not first_anchor or not last_anchor
                or pieces[0][-first_anchor:] != reference_pieces[0][-first_anchor:]
                or pieces[-1][:last_anchor] != reference_pieces[-1][:last_anchor]
            ):
                continue
            support = {
                "method": "declared_replacement_exon_sequence_concordance_v1",
                "accession": reference["accession"],
                "sequence_sha256": reference["sequence_sha256"],
                "original_sequence_sha256": record["sequence_sha256"],
                "first_junction_anchor_bases": first_anchor,
                "last_junction_anchor_bases": last_anchor,
                "terminal_poly_a_tail_length": len(sequence) - exons[-1]["end"],
            }
            record.update(
                exon_count=len(exons), exon_status="verified", exons=exons, exon_support=support,
                verification_note=(f"legacy_exon_features_confirmed_by_declared_replacement:{reference['accession']};"
                                   f"terminal_poly_a_tail_excluded:{support['terminal_poly_a_tail_length']}bp"),
            )
    return resolved


def _import_genbank(connection, path, source, seen):
    records = _genbank_records(Path(path).read_text(encoding="utf-8"))
    if source.get("expected_accession") and (
        len(records) != 1 or records[0]["accession"] != source["expected_accession"]
    ):
        raise StructureImportError("GenBank response does not match the requested accession/version")
    if source.get("expected_accessions") is not None and (
        len(records) != len(set(source["expected_accessions"]))
        or {record["accession"] for record in records} != set(source["expected_accessions"])
    ):
        raise StructureImportError("GenBank batch does not match the requested accession/version set")
    for record in records:
        accession = record["accession"]
        if accession in seen and seen[accession] != record:
            raise StructureImportError(f"Conflicting GenBank records for the same version: {accession}")
        seen[accession] = record
        if source.get("expected_gene") and record["gene"] != source["expected_gene"]:
            raise StructureImportError(f"GenBank response does not match the requested gene: {accession}")
        _ensure_transcript(connection, accession, record["gene"], authority=2,
                           gene_id=record["gene_id"], synonyms=record["gene_synonyms"])
        connection.execute(
            "UPDATE transcripts SET exon_count=?,exon_status=?,exon_source_id=?,exons=?,"
            "verification_note=? WHERE accession=?",
            (record["exon_count"], record["exon_status"], source["id"],
             _json(record["exons"]), record["verification_note"], accession),
        )
    return len(records)


def build_release(*, hg19, hg38, mane=None, genbank_dir=None, root=None, source_metadata=None):
    """Validate local source files, then publish a new immutable release.

    source_metadata keys are hg19, hg38, mane and genbank:<basename>. Values may
    supply the actual retrieval URL, known source release and observed_at. An
    unknown origin/release remains null; a file mtime is never a release date.
    """
    root = structure_root(root)
    root.mkdir(parents=True, exist_ok=True)
    metadata = source_metadata or {}
    if not isinstance(metadata, dict):
        raise StructureImportError("Source metadata must be an object")
    if "sources" in metadata:
        if not isinstance(metadata["sources"], list):
            raise StructureImportError("Source metadata sources must be an array")
        try:
            normalized = {f"genbank:{Path(item['path']).name}": item for item in metadata["sources"]}
        except (TypeError, KeyError) as exc:
            raise StructureImportError("Each source must be an object with a path") from exc
        if len(normalized) != len(metadata["sources"]):
            raise StructureImportError("Source metadata contains duplicate file names")
        metadata = {**metadata, **normalized}
    with (root / ".import.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StructureImportError("A transcript structure import is already running") from exc
        return _build_locked(root, hg19, hg38, mane, genbank_dir, metadata)


def _build_locked(root, hg19, hg38, mane, genbank_dir, metadata):
    release_id = uuid.uuid4().hex
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=releases))
    connection = None
    try:
        (temporary / "raw").mkdir()
        blobs = root / "blobs"
        blobs.mkdir(exist_ok=True)
        connection = sqlite3.connect(temporary / "structures.sqlite3")
        _schema(connection)
        sources, inputs = [], []
        for key, path, assembly in (("hg19", hg19, "GRCh37"), ("hg38", hg38, "GRCh38")):
            if not path:
                continue
            source = _source(key, path, metadata)
            connection.execute("INSERT INTO sources VALUES(?,?)", (source["id"], _json(source)))
            source["rows"] = _import_refgene(connection, path, assembly, source)
            sources.append(source)
            inputs.append((Path(path), source["sha256"]))
        # A conflicting symbol in an alignment file is evidence to retain, not
        # a reason to assign an arbitrary gene to the canonical transcript.
        connection.execute("""UPDATE transcripts SET gene='' WHERE accession IN (
            SELECT accession FROM mappings GROUP BY accession HAVING COUNT(DISTINCT gene)>1
        )""")
        if mane:
            source = _source("mane", mane, metadata)
            connection.execute("INSERT INTO sources VALUES(?,?)", (source["id"], _json(source)))
            source["rows"] = _import_mane(connection, mane, source)
            sources.append(source)
            inputs.append((Path(mane), source["sha256"]))
        if genbank_dir:
            location = Path(genbank_dir)
            paths = [location] if location.is_file() else sorted(
                path for path in location.iterdir()
                if path.is_file() and path.suffix.lower() in (".gb", ".gbk", ".txt", ".genbank")
            )
            if not paths:
                raise StructureImportError("No GenBank response files found")
            seen = {}
            for index, path in enumerate(paths):
                key = f"genbank:{path.name}"
                source = _source(key, path, metadata)
                raw_path = temporary / "raw" / f"{index:04d}-{path.name}"
                blob_path = blobs / f"{source['sha256']}.gb"
                if blob_path.exists():
                    if _sha256(blob_path) != source["sha256"]:
                        raise StructureImportError(f"Stored GenBank blob failed integrity validation: {blob_path.name}")
                else:
                    staged_blob = temporary / "new-blob"
                    shutil.copyfile(path, staged_blob)
                    if _sha256(staged_blob) != source["sha256"]:
                        raise StructureImportError(f"Source changed during copy: {path}")
                    with staged_blob.open("rb") as handle:
                        os.fsync(handle.fileno())
                    staged_blob.chmod(0o444)
                    staged_blob.rename(blob_path)
                    _sync_directory(blobs)
                os.link(blob_path, raw_path)
                source["raw_path"] = str(raw_path.relative_to(temporary))
                source["blob_path"] = str(blob_path.relative_to(root))
                connection.execute("INSERT INTO sources VALUES(?,?)", (source["id"], _json(source)))
                source["rows"] = _import_genbank(connection, raw_path, source, seen)
                sources.append(source)
            for record in resolve_supported_exon_counts(list(seen.values())):
                if record.get("exon_support"):
                    support = dict(record["exon_support"])
                    support["source_id"] = connection.execute(
                        "SELECT exon_source_id FROM transcripts WHERE accession=?", (support["accession"],),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE transcripts SET exon_count=?,exon_status=?,exons=?,verification_note=?,exon_support=? WHERE accession=?",
                        (record["exon_count"], record["exon_status"], _json(record["exons"]),
                         record["verification_note"], _json(support), record["accession"]),
                    )
        summary = {
            "transcripts": connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
            "verified": connection.execute("SELECT COUNT(*) FROM transcripts WHERE exon_status='verified'").fetchone()[0],
            "mappings": connection.execute("SELECT COUNT(*) FROM mappings").fetchone()[0],
            "mane_pairs": connection.execute("SELECT COUNT(*) FROM aliases").fetchone()[0],
        }
        summary["unverified"] = summary["transcripts"] - summary["verified"]
        if not summary["transcripts"]:
            raise StructureImportError("Cannot publish an empty transcript structure release")
        for path, checksum in inputs:
            if _sha256(path) != checksum:
                raise StructureImportError(f"Source changed during import: {path}")
        for source in sources:
            connection.execute("UPDATE sources SET data=? WHERE id=?", (_json(source), source["id"]))
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise StructureImportError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise StructureImportError("SQLite source/transcript foreign key validation failed")
        connection.close()
        connection = None
        manifest = {
            "schema_version": SCHEMA_VERSION, "release": release_id, "created_at": _now(),
            "summary": summary, "sources": sources,
            "database_sha256": _sha256(temporary / "structures.sqlite3"),
            "coordinate_conventions": {"mappings": "0-based half-open", "transcript_exons": "1-based closed"},
            "exon_policy": "Only complete human RefSeq RNA exon features are verified; genomic blocks are separate.",
        }
        _write_json(temporary / "manifest.json", manifest)
        (temporary / "structures.sqlite3").chmod(0o444)
        (temporary / "manifest.json").chmod(0o444)
        temporary.chmod(0o755)
        _sync_directory(temporary / "raw")
        _sync_directory(temporary)
        temporary.rename(releases / release_id)
        _sync_directory(releases)
        pointer = root / f".current-{release_id}.json"
        _write_json(pointer, {"release": release_id, "summary": summary, "created_at": manifest["created_at"]})
        os.replace(pointer, root / "current.json")
        _sync_directory(root)
        return manifest
    finally:
        if connection is not None:
            connection.close()
        # Only this import's unpublished staging directory is disposable.
        if temporary.exists():
            shutil.rmtree(temporary)


def _normalize_query(value):
    query = str(value or "").strip().upper()
    match = re.fullmatch(r"(NM|NR)_?(\d+)(\.\d+)?", query)
    return f"{match[1]}_{match[2]}{match[3] or ''}" if match else query


def search_structures(q="", limit=20, offset=0, *, root=None):
    """Indexed lookup; exact requested transcript versions never fall back."""
    query = _normalize_query(q)
    limit, offset = max(1, min(100, int(limit))), max(0, int(offset))
    result = {"ready": False, "release": None, "query": query, "total": 0,
              "results": [], "match_status": "not_found", "summary": {}}
    root = structure_root(root)
    try:
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result
    release_id = pointer.get("release", "")
    if not re.fullmatch(r"[a-f0-9]{32}", release_id):
        raise ValueError("Invalid transcript structure release pointer")
    directory = root / "releases" / release_id
    result.update(ready=True, release=release_id, summary=pointer["summary"])
    result["created_at"] = pointer["created_at"]
    connection = sqlite3.connect((directory / "structures.sqlite3").resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        parameters, status, via = [], "browse", "browse"
        if not query:
            where = "1=1"
        elif ACCESSION_QUERY.fullmatch(query):
            versioned = "." in query
            status = "exact" if versioned else "version_unspecified"
            if query.startswith("ENST"):
                column = "alias" if versioned else "base_alias"
                where = f"accession IN (SELECT accession FROM aliases WHERE {column}=?)"
                via = "mane"
            else:
                column = "accession" if versioned else "base_accession"
                where = f"{column}=?"
                via = "refseq"
            parameters = [query]
        else:
            where = "gene=? COLLATE NOCASE OR gene_id=? OR accession IN (SELECT accession FROM gene_aliases WHERE alias=? COLLATE NOCASE)"
            parameters, status, via = [query, query, query], "gene", "gene"
        result["total"] = connection.execute(f"SELECT COUNT(*) FROM transcripts WHERE {where}", parameters).fetchone()[0]
        result["match_status"] = status if result["total"] else "not_found"
        rows = connection.execute(
            f"SELECT * FROM transcripts WHERE {where} ORDER BY accession LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        ).fetchall()
        source_map = {}

        def get_source(source_id):
            if source_id is None:
                return None
            if source_id not in source_map:
                source = connection.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
                source_map[source_id] = json.loads(source["data"])
            return source_map[source_id]

        for row in rows:
            record = dict(row)
            record.pop("gene_authority")
            record["gene_aliases"] = json.loads(record["gene_aliases"])
            record["versioned"] = True
            record["matched_via"] = via
            record["transcript_exons"] = json.loads(record.pop("exons"))
            record["exon_support"] = json.loads(record["exon_support"])
            if record["exon_support"]:
                record["exon_support"]["source"] = get_source(record["exon_support"].pop("source_id"))
            record["exon_source"] = get_source(record.pop("exon_source_id"))
            record["candidate_exon_count"] = None
            record["ensembl_accessions"] = [alias[0] for alias in connection.execute(
                "SELECT alias FROM aliases WHERE accession=? ORDER BY alias", (row["accession"],),
            )]
            mappings, assembly_counts = [], {}
            for item in connection.execute("SELECT * FROM mappings WHERE accession=? ORDER BY assembly,chrom,start,id", (row["accession"],)):
                mapping = dict(item)
                mapping.pop("accession")
                mapping["exon_starts"] = json.loads(mapping["exon_starts"])
                mapping["exon_ends"] = json.loads(mapping["exon_ends"])
                mapping["source"] = get_source(mapping.pop("source_id"))
                mapping["differs_from_verified"] = (
                    mapping["raw_exon_count"] != record["exon_count"] if record["exon_count"] is not None else None
                )
                mappings.append(mapping)
                assembly_counts.setdefault(mapping["assembly"], []).append(mapping["raw_exon_count"])
            record["mappings"] = mappings
            record["mapping_gene_conflict"] = len({mapping["gene"] for mapping in mappings}) > 1
            record["assembly_summary"] = [
                {"assembly": assembly, "raw_exon_counts": sorted(set(counts)),
                 "has_conflict": len(set(counts)) > 1, "mapping_count": len(counts)}
                for assembly, counts in assembly_counts.items()
            ]
            result["results"].append(record)
    finally:
        connection.close()
    return result
