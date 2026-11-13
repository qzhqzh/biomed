import hashlib
import json
import tempfile
from pathlib import Path

from django.test import Client, SimpleTestCase, override_settings

from apps.annotations.knowledge.transcript_structures import (
    StructureImportError,
    _genbank_records,
    build_release,
    resolve_supported_exon_counts,
    search_structures,
)


def refgene(accession="NM_000348.4", gene="SRD5A2", count=5, chrom="chr2", origin=100):
    starts = [origin + index * 20 for index in range(count)]
    ends = [start + 10 for start in starts]
    return "\t".join(map(str, (
        0, accession, chrom, "+", starts[0], ends[-1], starts[0], ends[-1], count,
        ",".join(map(str, starts)) + ",", ",".join(map(str, ends)) + ",", 0, gene,
        "cmpl", "cmpl", ",".join("0" for _ in starts) + ",",
    ))) + "\n"


def genbank(accession="NM_000348.4", gene="SRD5A2", locations=None):
    locations = locations if locations is not None else [f"{index * 20 + 1}..{index * 20 + 20}" for index in range(5)]
    base = accession.split(".")[0]
    return (
        f"LOCUS       {base} 100 bp mRNA linear PRI 01-JAN-2026\n"
        f"ACCESSION   {base}\nVERSION     {accession}\n"
        "SOURCE      Homo sapiens (human)\n  ORGANISM  Homo sapiens\n"
        "FEATURES             Location/Qualifiers\n"
        "     source          1..100\n                     /organism=\"Homo sapiens\"\n"
        f"     gene            1..100\n                     /gene=\"{gene}\"\n"
        + "".join(f"     exon            {location}\n" for location in locations)
        + "ORIGIN\n        1 " + "a" * 100 + "\n//\n"
    )


MANE = (
    "#NCBI_GeneID\tsymbol\tRefSeq_nuc\tEnsembl_nuc\tMANE_status\n"
    "GeneID:6716\tSRD5A2\tNM_000348.4\tENST00000622030.2\tMANE Select\n"
)


class TranscriptStructureTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "structures"
        self.hg19, self.hg38 = self.directory / "hg19.txt", self.directory / "hg38.txt"
        self.hg19.write_text(refgene(count=6))
        self.hg38.write_text(refgene(count=5))
        self.genbank_dir = self.directory / "genbank"
        self.genbank_dir.mkdir()
        self.gb = self.genbank_dir / "NM_000348.4.gb"
        self.gb.write_text(genbank())
        self.mane = self.directory / "mane.txt"
        self.mane.write_text(MANE)
        settings_override = override_settings(TRANSCRIPT_STRUCTURE_DIR=self.root)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def build(self, **overrides):
        arguments = {"hg19": self.hg19, "hg38": self.hg38, "genbank_dir": self.genbank_dir, "mane": self.mane}
        arguments.update(overrides)
        return build_release(**arguments)

    def record(self, query="NM_000348.4"):
        return search_structures(query)["results"][0]

    def test_verified_count_is_separate_from_all_genomic_alignment_counts(self):
        manifest = self.build()
        record = self.record()
        self.assertEqual(record["exon_count"], 5)
        self.assertEqual(record["exon_status"], "verified")
        self.assertEqual(len(record["transcript_exons"]), 5)
        self.assertEqual([item["raw_exon_count"] for item in record["mappings"]], [6, 5])
        self.assertEqual([item["differs_from_verified"] for item in record["mappings"]], [True, False])
        raw = self.root / "releases" / manifest["release"] / record["exon_source"]["raw_path"]
        self.assertEqual(raw.read_bytes(), self.gb.read_bytes())
        self.assertEqual(record["exon_source"]["sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())

    def test_matching_alignment_counts_are_not_verified_biological_counts(self):
        self.hg19.write_text(refgene(count=5))
        self.build(genbank_dir=None)
        record = self.record()
        self.assertEqual(record["exon_status"], "unverified")
        self.assertIsNone(record["exon_count"])
        self.assertIsNone(record["candidate_exon_count"])
        self.assertIsNone(record["exon_source"])

    def test_exact_version_is_never_silently_substituted(self):
        self.hg19.write_text(refgene() + refgene(accession="NM_000348.3"))
        self.build()
        self.assertEqual(search_structures("nm000348.4")["total"], 1)
        self.assertEqual(search_structures("NM_000348.999")["results"], [])
        unversioned = search_structures("NM_000348")
        self.assertEqual(unversioned["total"], 2)
        self.assertEqual(unversioned["match_status"], "version_unspecified")

    def test_mane_transcript_mapping_preserves_both_versions_and_gene_identity(self):
        self.build()
        mapped = self.record("ENST00000622030.2")
        self.assertEqual(mapped["matched_via"], "mane")
        self.assertEqual(mapped["accession"], "NM_000348.4")
        self.assertEqual(mapped["gene_id"], "6716")
        self.assertEqual(mapped["ensembl_accessions"], ["ENST00000622030.2"])
        self.assertEqual(search_structures("ENST00000622030.999")["total"], 0)
        self.assertEqual(search_structures("ENST00000622030")["match_status"], "version_unspecified")
        self.assertEqual(search_structures("SRD5A2")["match_status"], "gene")
        self.assertEqual(search_structures("6716")["total"], 1)
        self.assertEqual(search_structures("NP_000339.2")["total"], 0)

    def test_all_loci_and_raw_gene_conflicts_are_retained(self):
        self.hg38.write_text(refgene() + refgene(count=2, origin=900, gene="old_symbol"))
        self.build()
        record = self.record()
        self.assertEqual(len(record["mappings"]), 3)
        self.assertTrue(record["mapping_gene_conflict"])
        self.assertEqual(record["gene"], "SRD5A2")
        self.assertEqual(record["assembly_summary"][1]["raw_exon_counts"], [2, 5])
        self.assertTrue(record["assembly_summary"][1]["has_conflict"])

    def test_bad_coordinate_arrays_do_not_change_current_release(self):
        self.build()
        pointer = (self.root / "current.json").read_bytes()
        fields = refgene().strip().split("\t")
        fields[8] = "6"
        self.hg38.write_text("\t".join(fields) + "\n")
        with self.assertRaisesRegex(StructureImportError, "exonCount"):
            self.build()
        self.assertEqual((self.root / "current.json").read_bytes(), pointer)
        self.assertEqual(len(list((self.root / "releases").iterdir())), 1)

    def test_legacy_scoreless_row_keeps_gene_position_and_explicit_format_warning(self):
        fields = refgene().strip().split("\t")
        fields.pop(11)
        self.hg38.write_text("\t".join(fields) + "\n")
        self.build()
        mapping = self.record()["mappings"][1]
        self.assertEqual(mapping["gene"], "SRD5A2")
        self.assertEqual(mapping["source_row_format"], "genePredExt_with_bin_missing_score")
        self.assertEqual(mapping["source_line"], 1)
        self.assertEqual(mapping["source"]["format_warnings"][0]["kind"], "missing_score_column")
        fields.pop(11)
        self.hg38.write_text("\t".join(fields) + "\n")
        with self.assertRaises(StructureImportError):
            self.build()

    def test_fuzzy_join_missing_overlap_and_duplicate_exons_do_not_verify(self):
        for locations in (
            ["<1..20", "21..100"], ["join(1..20,21..40)", "41..100"],
            ["1..20", "41..100"], ["1..50", "50..100"],
            ["1..20", "1..20", "21..100"], [],
        ):
            with self.subTest(locations=locations):
                self.gb.write_text(genbank(locations=locations))
                self.build()
                self.assertIsNone(self.record()["exon_count"])
                self.assertEqual(self.record()["exon_status"], "unverified")
                self.assertEqual(self.record()["transcript_exons"], [])

    def test_non_human_truncated_and_wrong_accession_records_are_rejected(self):
        for text in (
            genbank().replace("Homo sapiens", "Mus musculus"),
            genbank().removesuffix("//\n"),
            genbank().replace("VERSION     NM_000348.4", "VERSION     NM_999999.4"),
            genbank().replace("a" * 100, "a" * 99),
            genbank().replace('/gene="SRD5A2"', '/gene="UNRELATED"'),
        ):
            with self.subTest(text=text[:100]):
                self.gb.write_text(text)
                with self.assertRaises(StructureImportError):
                    self.build()
                self.assertFalse((self.root / "current.json").exists())

    def test_supported_poly_a_tail_does_not_become_an_extra_exon(self):
        text = genbank(locations=["1..20", "21..80"])
        text = text.replace("ORIGIN\n", "     polyA_site      80\nORIGIN\n")
        self.gb.write_text(text)
        self.build()
        record = self.record()
        self.assertEqual(record["exon_count"], 2)
        self.assertEqual(record["transcript_exons"][-1]["end"], 80)
        self.assertEqual(record["verification_note"], "terminal_poly_a_tail_excluded:20bp;polyA_site:80")

    def test_poly_a_requires_site_support_contiguous_exons_and_pure_a_tail(self):
        baseline = genbank(locations=["1..20", "21..80"])
        for text in (
            baseline,
            baseline.replace("ORIGIN\n", "     polyA_site      <80\nORIGIN\n"),
            baseline.replace("ORIGIN\n", "     polyA_site      80\nORIGIN\n").replace("a" * 100, "a" * 99 + "c"),
            baseline.replace("ORIGIN\n", "     polyA_site      80\nORIGIN\n").replace("21..80", "22..80"),
            baseline.replace("ORIGIN\n", "     polyA_site      70\nORIGIN\n").replace("a" * 100, "a" * 74 + "c" + "a" * 25),
        ):
            with self.subTest(text=text):
                self.gb.write_text(text)
                self.build()
                self.assertEqual(self.record()["exon_status"], "unverified")
                self.assertIsNone(self.record()["exon_count"])

    def test_poly_a_site_can_be_inside_the_same_terminal_a_run(self):
        self.gb.write_text(genbank(locations=["1..20", "21..80"]).replace("ORIGIN\n", "     polyA_site      75\nORIGIN\n"))
        self.build()
        self.assertEqual(self.record()["exon_count"], 2)
        self.assertIn("polyA_site:75", self.record()["verification_note"])

    def test_official_gene_rename_requires_matching_gene_id_and_explicit_synonym(self):
        renamed = genbank(gene="RENAMED").replace(
            '                     /gene="RENAMED"\n',
            '                     /gene="RENAMED"\n                     /gene_synonym="SRD5A2; OTHER"\n'
            '                     /db_xref="GeneID:6716"\n',
        )
        self.gb.write_text(renamed)
        self.build()
        record = self.record()
        self.assertEqual(record["gene"], "SRD5A2")
        self.assertEqual(record["gene_id"], "6716")
        self.assertIn("RENAMED", record["gene_aliases"])
        self.assertEqual(search_structures("RENAMED")["total"], 1)
        for invalid in (
            renamed.replace('                     /db_xref="GeneID:6716"\n', ""),
            renamed.replace("GeneID:6716", "GeneID:1"),
            renamed.replace('/gene_synonym="SRD5A2; OTHER"', '/gene_synonym="UNRELATED"'),
        ):
            with self.subTest(invalid=invalid):
                self.gb.write_text(invalid)
                with self.assertRaises(StructureImportError):
                    self.build()

    def legacy_pair(self):
        original = genbank(accession="NM_000348.3", locations=["1..20", "21..60", "61..90"])
        reference = genbank(locations=["1..20", "21..60", "61..100"])
        original = original.replace("FEATURES", "COMMENT     [WARNING] On Jan 1, 2026 this sequence was replaced by NM_000348.4.\nFEATURES")
        return tuple(text.replace('/gene="SRD5A2"', '/gene="SRD5A2"\n                     /db_xref="GeneID:6716"')
                     for text in (original, reference))

    def test_declared_replacement_checks_old_features_and_preserves_both_sources(self):
        original, reference = self.legacy_pair()
        (self.genbank_dir / "NM_000348.3.gb").write_text(original)
        self.gb.write_text(reference)
        self.build()
        old = self.record("NM_000348.3")
        self.assertEqual(old["exon_count"], 3)
        self.assertEqual(old["transcript_exons"][-1]["end"], 90)
        self.assertEqual(self.record()["transcript_exons"][-1]["end"], 100)
        self.assertEqual(old["exon_support"]["accession"], "NM_000348.4")
        self.assertNotEqual(old["exon_source"]["sha256"], old["exon_support"]["source"]["sha256"])
        self.assertEqual(old["exon_support"]["terminal_poly_a_tail_length"], 10)

    def test_replacement_cannot_repair_missing_features_internal_changes_or_gaps(self):
        original, reference = self.legacy_pair()
        for bad in (
            original.replace("     exon            21..60\n", ""),
            original.replace("21..60", "22..60"),
            original.replace("a" * 100, "a" * 39 + "c" + "a" * 60),
            original.replace("a" * 100, "c" + "a" * 99),
            original.replace("a" * 100, "a" * 69 + "c" + "a" * 30),
        ):
            with self.subTest(bad=bad):
                records = _genbank_records(bad + reference)
                result = resolve_supported_exon_counts(records)
                self.assertEqual(result[0]["exon_status"], "unverified")
                self.assertIsNone(result[0]["exon_count"])

    def test_replacement_requires_official_link_same_base_and_gene_id(self):
        original, reference = self.legacy_pair()
        for bad_original, bad_reference in (
            (original.replace("[WARNING]", "[NOTE]"), reference),
            (original.replace("GeneID:6716", "GeneID:1"), reference),
            (original.replace('                     /db_xref="GeneID:6716"\n', ""), reference),
            (original.replace("replaced by NM_000348.4", "replaced by NM_999999.4"), reference.replace("NM_000348", "NM_999999")),
        ):
            with self.subTest(original=bad_original):
                result = resolve_supported_exon_counts(_genbank_records(bad_original + bad_reference))
                self.assertIsNone(result[0]["exon_count"])

    def test_replacement_reference_must_be_independently_verified(self):
        original, reference = self.legacy_pair()
        middle = original.replace("NM_000348.3", "NM_000348.2").replace("replaced by NM_000348.4", "replaced by NM_000348.3")
        records = _genbank_records(original + reference + middle)
        result = resolve_supported_exon_counts(records)
        self.assertEqual(result[0]["exon_status"], "verified")
        self.assertEqual(result[2]["exon_status"], "unverified")
        self.assertEqual(records[0]["exon_status"], "unverified")

    def test_conflicting_structures_for_one_version_cannot_overwrite(self):
        (self.genbank_dir / "duplicate.gb").write_text(genbank(locations=["1..50", "51..100"]))
        with self.assertRaisesRegex(StructureImportError, "Conflicting GenBank"):
            self.build()
        self.assertFalse((self.root / "current.json").exists())

    def test_release_is_immutable_and_old_data_remains_available(self):
        first = self.build()
        previous = self.root / "releases" / first["release"] / "structures.sqlite3"
        self.assertEqual(previous.parent.stat().st_mode & 0o777, 0o755)
        checksum = hashlib.sha256(previous.read_bytes()).hexdigest()
        self.hg38.write_text(refgene() + refgene(accession="NM_000348.3"))
        second = self.build()
        self.assertNotEqual(first["release"], second["release"])
        self.assertEqual(hashlib.sha256(previous.read_bytes()).hexdigest(), checksum)
        self.assertEqual(search_structures()["release"], second["release"])
        first_raw = next((previous.parent / "raw").iterdir())
        second_raw = next((self.root / "releases" / second["release"] / "raw").iterdir())
        self.assertEqual(first_raw.stat().st_ino, second_raw.stat().st_ino)
        self.assertEqual(len(list((self.root / "blobs").iterdir())), 1)

    def test_source_metadata_hash_identity_and_retrieval_time_are_checked(self):
        source = {"path": str(self.gb), "accession": "NM_000348.4", "gene": "SRD5A2",
                  "sha256": hashlib.sha256(self.gb.read_bytes()).hexdigest(),
                  "url": "https://example.org/exact-record", "observed_at": "2026-10-28T01:00:00Z",
                  "time_basis": "response_fully_received_at"}
        self.build(source_metadata={"sources": [source]})
        evidence = self.record()["exon_source"]
        self.assertEqual(evidence["retrieved_at"], source["observed_at"])
        self.assertEqual(evidence["url"], source["url"])
        self.assertIsNone(evidence["release"])
        with self.assertRaisesRegex(StructureImportError, "SHA-256"):
            self.build(source_metadata={"sources": [{**source, "sha256": "bad"}]})
        with self.assertRaisesRegex(StructureImportError, "requested accession"):
            self.build(source_metadata={"sources": [{**source, "accession": "NM_000348.999"}]})
        self.build(source_metadata={"sources": [{**source, "time_basis": "cache_mtime_not_retrieval"}]})
        self.assertIsNone(self.record()["exon_source"]["retrieved_at"])

    def test_multirecord_genbank_response_requires_exact_requested_version_set(self):
        self.gb.write_text(genbank() + genbank(accession="NM_000348.3"))
        self.build(source_metadata={"sources": [{"path": str(self.gb), "accessions": ["NM_000348.4", "NM_000348.3"]}]})
        self.assertEqual(search_structures()["summary"]["verified"], 2)
        with self.assertRaisesRegex(StructureImportError, "requested accession/version set"):
            self.build(source_metadata={"sources": [{"path": str(self.gb), "accessions": ["NM_000348.4"]}]})

    def test_api_does_not_fall_back_for_unavailable_exact_version(self):
        self.build()
        response = Client().get("/api/v1/knowledge/transcript-structures/", {"q": "NM_000348.999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_api_exposes_verified_count_and_both_assembly_mappings(self):
        self.build()
        response = Client().get("/api/v1/knowledge/transcript-structures/", {"q": "NM_000348.4"})
        self.assertEqual(response.status_code, 200)
        record = response.json()["results"][0]
        self.assertEqual(record["exon_count"], 5)
        self.assertEqual([item["raw_exon_count"] for item in record["mappings"]], [6, 5])

    def test_missing_release_returns_unready_and_pagination_is_bounded(self):
        self.assertFalse(search_structures()["ready"])
        self.hg19.write_text("".join(refgene(accession=f"NM_{index:06d}.1") for index in range(105)))
        self.build(mane=None, genbank_dir=None)
        result = search_structures(limit=500)
        self.assertEqual(len(result["results"]), 100)
        self.assertEqual(result["total"], 106)
        self.assertEqual(len(search_structures(limit=500, offset=100)["results"]), 6)

    def test_command_automatically_uses_source_manifest(self):
        from io import StringIO

        from django.core.management import call_command

        metadata = {"sources": [{"path": str(self.gb), "accession": "NM_000348.4", "url": "https://example.org/source"}]}
        (self.genbank_dir / "sources.json").write_text(json.dumps(metadata))
        output = StringIO()
        call_command("import_transcript_structures", hg19=self.hg19, hg38=self.hg38,
                     genbank_dir=self.genbank_dir, stdout=output)
        self.assertEqual(self.record()["exon_source"]["url"], "https://example.org/source")
        self.assertEqual(json.loads(output.getvalue())["summary"]["verified"], 1)
