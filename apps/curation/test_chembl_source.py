import copy
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from apps.curation import chembl_source as source


def molecule(identifier, parent=None):
    return {
        "molecule_chembl_id": identifier,
        "pref_name": identifier,
        "molecule_hierarchy": {"parent_chembl_id": parent or identifier},
        "molecule_synonyms": [{"molecule_synonym": "中文名称", "syn_type": "OTHER"}],
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail_once = None
        self.statuses = []
        self.mutate = None
        self.data = {
            "mechanism": [
                {
                    "mec_id": i,
                    "molecule_chembl_id": f"CHEMBL{i}",
                    "parent_molecule_chembl_id": "CHEMBL9" if i == 1 else f"CHEMBL{i}",
                    "target_chembl_id": "CHEMBL20" if i < 3 else None,
                    "action_type": "INHIBITOR",
                    "direct_interaction": 1,
                    "disease_efficacy": 0,
                    "variant_sequence": {"mutation": "T790M"},
                    "mechanism_refs": [{"ref_type": "PubMed", "ref_id": "123"}],
                }
                for i in range(1, 4)
            ],
            "molecule": [
                molecule("CHEMBL1", "CHEMBL9"),
                molecule("CHEMBL2"),
                molecule("CHEMBL3"),
                molecule("CHEMBL9"),
            ],
            "target": [
                {
                    "target_chembl_id": "CHEMBL20",
                    "pref_name": "Target",
                    "target_components": [
                        {
                            "accession": "P00533",
                            "target_component_xrefs": [
                                {"xref_src_db": "EnsemblGene", "xref_id": "ENSG1"}
                            ],
                        }
                    ],
                }
            ],
        }

    def __call__(self, url, limit):
        self.calls.append(url)
        endpoint = urlsplit(url).path.rsplit("/", 1)[1].split(".")[0]
        if self.fail_once == endpoint:
            self.fail_once = None
            raise URLError("temporary failure")
        if endpoint == "status":
            result = (
                self.statuses.pop(0)
                if self.statuses
                else {
                    "status": "UP",
                    "chembl_db_version": "ChEMBL_37",
                    "chembl_release_date": "2026-05-01",
                }
            )
        else:
            params = parse_qs(urlsplit(url).query)
            rows = copy.deepcopy(self.data[endpoint])
            if endpoint != "mechanism":
                identifiers = params[f"{endpoint}_chembl_id__in"][0].split(",")
                rows = [
                    row for row in rows if row[f"{endpoint}_chembl_id"] in identifiers
                ]
            offset, size = int(params["offset"][0]), int(params["limit"][0])
            result = {
                source.COLLECTIONS[endpoint][0]: rows[offset : offset + size],
                "page_meta": {
                    "offset": offset,
                    "limit": size,
                    "total_count": len(rows),
                    "next": "next" if offset + size < len(rows) else None,
                },
            }
        if self.mutate:
            self.mutate(endpoint, result)
        return json.dumps(result).encode()


class ChEMBLSnapshotTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.api = FakeAPI()
        for attribute, value in (("PAGE_SIZE", 2), ("BATCH_SIZE", 2)):
            mock = patch.object(source, attribute, value)
            mock.start()
            self.addCleanup(mock.stop)
        sleep = patch.object(source.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_snapshot(self, **kwargs):
        return source.fetch_snapshot(self.directory, transport=self.api, **kwargs)

    def test_complete_snapshot_preserves_source_assertions_and_provenance(self):
        manifest = self.run_snapshot()
        self.assertEqual(
            manifest["counts"], {"mechanisms": 3, "molecules": 4, "targets": 1}
        )
        self.assertEqual(manifest["version"], "ChEMBL_37")
        self.assertEqual(manifest["license"]["name"], "CC BY-SA 3.0")
        self.assertFalse(manifest["clinical_efficacy_verified"])
        self.assertEqual(manifest["cjk_synonym_molecules"], 4)
        self.assertEqual(manifest["cjk_synonym_records"], 4)
        mechanisms = json.loads((self.directory / "mechanisms.json").read_text())
        self.assertEqual(mechanisms[0]["variant_sequence"], {"mutation": "T790M"})
        self.assertEqual(mechanisms[0]["disease_efficacy"], 0)
        receipt = mechanisms[0]["_source"]
        self.assertEqual(
            source.sha256((self.directory / receipt["file"]).read_bytes()),
            receipt["sha256"],
        )
        self.assertEqual(
            len([url for url in self.api.calls if "/mechanism." in url]), 2
        )
        self.assertEqual(source.validate_completed(self.directory), manifest)

    def test_completed_snapshot_never_refetches_or_changes(self):
        original = self.run_snapshot()
        calls = len(self.api.calls)
        self.assertEqual(self.run_snapshot(), original)
        self.assertEqual(len(self.api.calls), calls)
        (self.directory / "molecules.json").write_text("[]")
        with self.assertRaisesRegex(source.SnapshotError, "checksum"):
            self.run_snapshot()

    def test_interruption_resumes_verified_pages_and_rechecks_version(self):
        original = source.entity_batches
        with (
            patch.object(
                source, "entity_batches", side_effect=RuntimeError("interrupted")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.run_snapshot()
        self.assertFalse((self.directory / "manifest.json").exists())
        mechanisms_calls = len([url for url in self.api.calls if "/mechanism." in url])
        with patch.object(source, "entity_batches", wraps=original):
            self.run_snapshot()
        self.assertEqual(
            len([url for url in self.api.calls if "/mechanism." in url]),
            mechanisms_calls,
        )
        self.assertEqual(len([url for url in self.api.calls if "/status." in url]), 3)

    def test_version_change_blocks_final_manifest(self):
        self.api.statuses = [
            {
                "status": "UP",
                "chembl_db_version": "ChEMBL_37",
                "chembl_release_date": "2026-05-01",
            },
            {
                "status": "UP",
                "chembl_db_version": "ChEMBL_38",
                "chembl_release_date": "2026-11-01",
            },
        ]
        with self.assertRaisesRegex(source.SnapshotError, "version changed"):
            self.run_snapshot()
        self.assertFalse((self.directory / "manifest.json").exists())

    def test_changed_page_total_and_duplicate_mechanism_are_rejected(self):
        def mutation(endpoint, value):
            if endpoint == "mechanism" and value["page_meta"]["offset"] == 2:
                value["mechanisms"][0]["mec_id"] = 1

        self.api.mutate = mutation
        with self.assertRaisesRegex(source.SnapshotError, "duplicate"):
            self.run_snapshot()
        self.assertFalse((self.directory / "manifest.json").exists())

    def test_missing_referenced_entity_cannot_publish_partial_release(self):
        self.api.data["target"] = []
        with self.assertRaisesRegex(source.SnapshotError, "incomplete"):
            self.run_snapshot()
        self.assertFalse((self.directory / "manifest.json").exists())

    def test_budget_is_persistent_and_scope_cannot_expand(self):
        with self.assertRaisesRegex(source.SnapshotError, "budget exhausted"):
            self.run_snapshot(max_requests=2)
        calls = len(self.api.calls)
        with self.assertRaisesRegex(source.SnapshotError, "budget exhausted"):
            self.run_snapshot(max_requests=2)
        self.assertEqual(len(self.api.calls), calls)
        with self.assertRaisesRegex(source.SnapshotError, "configuration changed"):
            self.run_snapshot(max_requests=20)

    def test_transient_failure_retries_and_does_not_lose_completed_pages(self):
        self.api.fail_once = "mechanism"
        manifest = self.run_snapshot()
        self.assertEqual(manifest["counts"]["mechanisms"], 3)
        attempts = json.loads((self.directory / "checkpoint.json").read_text())[
            "attempts"
        ]
        self.assertEqual(
            len([item for item in attempts if item["state"] == "error"]), 1
        )

    def test_raw_cache_tampering_blocks_resume(self):
        with (
            patch.object(
                source, "entity_batches", side_effect=RuntimeError("interrupted")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.run_snapshot()
        checkpoint = json.loads((self.directory / "checkpoint.json").read_text())
        receipt = next(
            value for key, value in checkpoint["cache"].items() if "/mechanism." in key
        )
        (self.directory / receipt["file"]).write_text("{}")
        with self.assertRaisesRegex(source.SnapshotError, "checksum"):
            self.run_snapshot()

    def test_url_scope_prevents_full_molecule_database_and_other_hosts(self):
        for url in (
            source.API + "molecule.json?limit=500",
            source.API + "activity.json",
            "https://www.ebi.ac.uk.example.com/chembl/api/data/status.json",
            "http://www.ebi.ac.uk/chembl/api/data/status.json",
        ):
            with self.subTest(url=url), self.assertRaises(source.SnapshotError):
                source.allowed(url)


class ChEMBLReaderTests(TestCase):
    def test_reader_enforces_256kib_application_rate(self):
        clock = [0.0]
        response = io.BytesIO(b"x" * source.RATE)
        response.headers = {}
        opener = MagicMock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            client = source.SnapshotHTTP(directory)
            with (
                patch.object(source, "build_opener", return_value=opener),
                patch.object(source.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(
                    source.time,
                    "sleep",
                    side_effect=lambda seconds: clock.__setitem__(
                        0, clock[0] + seconds
                    ),
                ),
            ):
                attempt = {}
                data = client.read(source.API + "status.json", source.RATE + 1, attempt)
        self.assertEqual(len(data), source.RATE)
        self.assertGreaterEqual(clock[0], 1)
        self.assertEqual(attempt["charged_bytes"], source.RATE)
