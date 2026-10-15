from django.test import SimpleTestCase, Client, override_settings
from pathlib import Path
import tempfile
from unittest.mock import patch

from apps.annotations.normalize import normalize_variant
from apps.annotations.engines import vep as vep_engine
from apps.annotations.engines.vep import parse_vep_output
from apps.annotations.engines.annovar import (
    parse_multianno,
    to_avinput_line,
)


class NormalizeTests(SimpleTestCase):
    def test_colon_format(self):
        self.assertEqual(
            normalize_variant("17:43092951 G>A"),
            "17 43092951 43092951 G/A +",
        )

    def test_chr_prefix(self):
        self.assertEqual(
            normalize_variant("chr13:32906732 G>A"),
            "13 32906732 32906732 G/A +",
        )

    def test_hgvs_g(self):
        self.assertEqual(
            normalize_variant("17:g.43092951G>A"),
            "17 43092951 43092951 G/A +",
        )


class VepRunnerModeTests(SimpleTestCase):
    @override_settings(VEP_MODE="local", VEP_BIN="/nonexistent/vep")
    def test_local_mode_not_ready_without_bin(self):
        self.assertFalse(vep_engine.local_bin_ready())
        self.assertFalse(vep_engine.runner_ready())

    @override_settings(VEP_MODE="auto", VEP_BIN="/nonexistent/vep")
    def test_auto_uses_docker_when_no_local_bin(self):
        self.assertFalse(vep_engine.local_bin_ready())
        self.assertTrue(vep_engine._use_docker())


class ParseVepTests(SimpleTestCase):
    def test_parse_pick_consequence(self):
        raw = [
            {
                "input": "17 43092951 43092951 G/A +",
                "transcript_consequences": [
                    {
                        "variant_allele": "A",
                        "gene_symbol": "BRCA1",
                        "transcript_id": "ENST00000",
                        "consequence_terms": ["missense_variant"],
                        "impact": "MODERATE",
                        "hgvsc": "ENST00000:c.1G>A",
                        "hgvsp": "ENSP00000:p.Gly1Arg",
                        "biotype": "protein_coding",
                        "canonical": 1,
                    }
                ],
            }
        ]
        results = parse_vep_output(raw, {"17 43092951 43092951 G/A +": "17:43092951 G>A"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].gene, "BRCA1")
        self.assertEqual(results[0].input, "17:43092951 G>A")
        self.assertEqual(results[0].canonical, "YES")

    def test_parse_prefers_mane_over_other_isoforms(self):
        raw = [
            {
                "input": "17 7577120 7577120 C/T +",
                "transcript_consequences": [
                    {
                        "variant_allele": "T",
                        "gene_symbol": "TP53",
                        "transcript_id": "ENST00000619186",
                        "consequence_terms": ["missense_variant"],
                        "hgvsc": "ENST00000619186:c.422G>A",
                        "hgvsp": "ENSP000004:p.Arg141His",
                        "canonical": 0,
                    },
                    {
                        "variant_allele": "T",
                        "gene_symbol": "TP53",
                        "transcript_id": "ENST00000269305",
                        "consequence_terms": ["missense_variant"],
                        "hgvsc": "ENST00000269305:c.818G>A",
                        "hgvsp": "ENSP00000269305:p.Arg273His",
                        "canonical": 1,
                        "mane_select": 1,
                    },
                ],
            }
        ]
        results = parse_vep_output(raw, {"17 7577120 7577120 C/T +": "17:7577120 C>T"})
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row.feature, "ENST00000269305")
        self.assertIn("c.818G>A", row.cdot or "")
        self.assertEqual(len(row.transcripts), 2)
        self.assertTrue(row.transcripts[0]["preferred"])
        self.assertEqual(row.transcripts[0]["mane_status"], "select")
        self.assertEqual(
            row.details.get("transcript_pick", {}).get("reason"), "mane_select"
        )

    def test_parse_prefers_mane_nm_over_mane_enst(self):
        """Within MANE Select, prefer RefSeq NM when merged cache returns both."""
        raw = [
            {
                "input": "17 7577120 7577120 C/T +",
                "transcript_consequences": [
                    {
                        "variant_allele": "T",
                        "gene_symbol": "TP53",
                        "transcript_id": "ENST00000269305",
                        "consequence_terms": ["missense_variant"],
                        "hgvsc": "ENST00000269305.4:c.818G>A",
                        "hgvsp": "ENSP00000269305.4:p.Arg273His",
                        "canonical": 1,
                        "mane_select": 1,
                    },
                    {
                        "variant_allele": "T",
                        "gene_symbol": "TP53",
                        "transcript_id": "NM_000546.6",
                        "consequence_terms": ["missense_variant"],
                        "hgvsc": "NM_000546.6:c.818G>A",
                        "hgvsp": "NP_000537.3:p.Arg273His",
                        "canonical": 0,
                        "mane_select": 1,
                    },
                ],
            }
        ]
        results = parse_vep_output(raw, {"17 7577120 7577120 C/T +": "17:7577120 C>T"})
        row = results[0]
        self.assertTrue((row.feature or "").startswith("NM_000546"))
        self.assertIn("c.818G>A", row.cdot or "")
        self.assertEqual(
            row.details.get("transcript_pick", {}).get("reason"), "mane_select_nm"
        )


class AnnovarUnitTests(SimpleTestCase):
    def test_to_avinput(self):
        original, line = to_avinput_line("17:43092951 G>A")
        self.assertEqual(original, "17:43092951 G>A")
        self.assertEqual(line, "17\t43092951\t43092951\tG\tA")

    def test_parse_multianno(self):
        content = (
            "Chr\tStart\tEnd\tRef\tAlt\tFunc.refGeneWithVer\tGene.refGeneWithVer\t"
            "GeneDetail.refGeneWithVer\tExonicFunc.refGeneWithVer\tAAChange.refGeneWithVer\n"
            "chr17\t43092951\t43092951\tG\tA\texonic\tBRCA1\t.\tmissense\t"
            "BRCA1:NM_007294:exon10:c.1G>A:p.G1R\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.hg38_multianno.txt"
            path.write_text(content, encoding="utf-8")
            results = parse_multianno(
                path, {"17:43092951:43092951:G:A": "17:43092951 G>A"}
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].gene, "BRCA1")
        self.assertEqual(results[0].engine, "annovar")
        self.assertIn("c.1G>A", results[0].cdot or "")

    @patch("apps.annotations.engines.transcript_prefer.knowledge_store.lookup_transcript")
    def test_parse_multianno_prefers_mane_select(self, lookup_transcript):
        """When AAChange lists a non-MANE isoform first, prefer MANE Select NM."""
        lookup_transcript.side_effect = lambda accession: (
            {"mane": True, "mane_status": "select"} if accession == "NM_000546" else None
        )
        # NM_001126115 is non-MANE; NM_000546 is MANE Select for TP53.
        aachange = (
            "TP53:NM_001126115.1:exon4:c.422G>A:p.R141H,"
            "TP53:NM_000546.6:exon8:c.818G>A:p.R273H,"
            "TP53:NM_001126112.3:exon4:c.422G>A:p.R141H"
        )
        content = (
            "Chr\tStart\tEnd\tRef\tAlt\tFunc.refGeneWithVer\tGene.refGeneWithVer\t"
            "GeneDetail.refGeneWithVer\tExonicFunc.refGeneWithVer\tAAChange.refGeneWithVer\n"
            f"chr17\t7577120\t7577120\tC\tT\texonic\tTP53\t.\tnonsynonymous SNV\t{aachange}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.hg19_multianno.txt"
            path.write_text(content, encoding="utf-8")
            results = parse_multianno(
                path, {"17:7577120:7577120:C:T": "17:7577120 C>T"}
            )
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row.gene, "TP53")
        self.assertTrue(
            (row.feature or "").startswith("NM_000546"),
            msg=f"expected MANE NM_000546, got {row.feature}",
        )
        self.assertIn("c.818G>A", row.cdot or "")
        self.assertIn("p.R273H", row.protein or "")
        self.assertEqual(row.canonical, "YES")
        self.assertEqual(row.details.get("transcript_pick", {}).get("reason"), "mane_select")
        self.assertEqual(len(row.transcripts), 3)
        self.assertTrue(row.transcripts[0].get("preferred"))
        self.assertTrue((row.transcripts[0].get("feature") or "").startswith("NM_000546"))
        features = [t.get("feature") for t in row.transcripts]
        self.assertTrue(any((f or "").startswith("NM_001126115") for f in features))


class ApiSmokeTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_health_live(self):
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_engines_include_vep_and_annovar(self):
        resp = self.client.get("/api/v1/engines/")
        self.assertEqual(resp.status_code, 200)
        ids = {e["id"] for e in resp.json()["engines"]}
        self.assertEqual(ids, {"vep", "annovar"})

    def test_annotations_validation(self):
        resp = self.client.post(
            "/api/v1/annotations/",
            data={"variants": [], "assembly": "GRCh37"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    @override_settings(
        ANNOVAR_PUBLIC_ENABLED=True,
        ANNOVAR_MODE="local",
        ANNOVAR_TABLE_BIN="/nonexistent/table_annovar.pl",
        BIOTOOLS_API_KEYS="",
        BIOTOOLS_REQUIRE_API_KEY=False,
    )
    def test_annovar_not_ready_returns_503(self):
        resp = self.client.post(
            "/api/v1/annotations/",
            data={
                "engine": "annovar",
                "assembly": "GRCh38",
                "variants": ["17:43092951 G>A"],
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 503)

    @override_settings(ANNOVAR_PUBLIC_ENABLED=False, BIOTOOLS_API_KEYS="", BIOTOOLS_REQUIRE_API_KEY=False)
    def test_annovar_license_gate_returns_403(self):
        resp = self.client.post(
            "/api/v1/annotations/",
            data={
                "engine": "annovar",
                "assembly": "GRCh38",
                "variants": ["17:43092951 G>A"],
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(BIOTOOLS_API_KEYS="secret-key", BIOTOOLS_REQUIRE_API_KEY=False)
    def test_annotations_require_api_key(self):
        resp = self.client.post(
            "/api/v1/annotations/",
            data={
                "engine": "vep",
                "assembly": "GRCh37",
                "variants": ["17:43092951 G>A"],
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(BIOTOOLS_API_KEYS="secret-key", BIOTOOLS_REQUIRE_API_KEY=False)
    def test_request_id_header_present(self):
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers.get("X-Request-ID"))


class JobsApiTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(
        BIOTOOLS_API_KEYS="",
        BIOTOOLS_REQUIRE_API_KEY=False,
        BIOTOOLS_JOB_COOLDOWN_SECONDS=10,
    )
    def test_job_submit_cooldown(self):
        from unittest.mock import patch

        from apps.annotations.models import AnnotationJob

        def fake_create(**kwargs):
            return AnnotationJob(
                id="00000000-0000-0000-0000-000000000001",
                status=AnnotationJob.Status.QUEUED,
                assembly=kwargs["assembly"],
                engines=kwargs["engines"],
                variants=kwargs["variants"],
                variant_count=len(kwargs["variants"]),
            )

        payload = {
            "engines": ["vep"],
            "assembly": "GRCh37",
            "variants": ["17:7577120 C>T"],
        }
        with patch(
            "apps.annotations.services.jobs.create_and_enqueue",
            side_effect=fake_create,
        ):
            first = self.client.post(
                "/api/v1/jobs/",
                data=payload,
                content_type="application/json",
            )
            self.assertEqual(first.status_code, 202)
            second = self.client.post(
                "/api/v1/jobs/",
                data=payload,
                content_type="application/json",
            )
            self.assertEqual(second.status_code, 429)
            body = second.json()
            self.assertEqual(body["error"]["code"], "submit_too_soon")
            self.assertIn("retry_after", body["error"])
