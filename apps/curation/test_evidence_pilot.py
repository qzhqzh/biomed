"""Offline experiment behavior against disposable source and output fixtures."""

import hashlib
import io
import json
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

from apps.curation import evidence_pilot as pilot
from apps.curation.decision import digest
from apps.curation.pilot_candidates import locate_candidates


class EvidencePilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "experiment"
        self.folder.mkdir()

    def write_snapshot(self, count=3):
        documents = []
        for rank in range(1, count + 1):
            metadata = {
                "title": f"Paper {rank}",
                "abstract": "Drug treatment showed no survival benefit.",
                "mesh": [],
                "publication_types": ["Journal Article"],
            }
            model_input = {
                "pmid": str(100000 + rank),
                "version_id": f"metadata-{rank}",
                "source_sha256": "a" * 64,
                "input_sha256": digest(metadata),
                "metadata": metadata,
            }
            passages = [
                {
                    "id": f"abstract:{rank}",
                    "version_id": model_input["version_id"],
                    "parser_version": "pubmed-metadata-v3",
                    "ordinal": 0,
                    "locator": "PubMed abstract",
                    "text": metadata["abstract"],
                    "kind": "abstract",
                }
            ]
            documents.append(
                {
                    "rank": rank,
                    "material_id": f"paper-{rank}",
                    "pmid": model_input["pmid"],
                    "title": metadata["title"],
                    "created_at": "2026-10-15T00:00:00+00:00",
                    "source_key": "pubmed:" + model_input["pmid"],
                    "versions": [],
                    "metadata_version_id": model_input["version_id"],
                    "fulltext_version_id": None,
                    "metadata": metadata,
                    "model_input": model_input,
                    "passages": passages,
                    "passages_sha256": digest(passages),
                    "source_issues": ["parsed_fulltext_missing"],
                    "input_scope": "abstract_only",
                    "historical_exposures": [],
                    "evaluation_role": "development_pipeline",
                    "reference_status": "unannotated",
                    "independent_holdout": False,
                    "rule_baseline": {"scope": "uncertain", "method": "weak_rules"},
                }
            )
        snapshot = {
            "version": pilot.VERSION,
            "created_at": "2026-10-22T00:00:00+00:00",
            "database": str(self.root / "source" / "db.sqlite3"),
            "library": str(self.root / "library"),
            "limit": count,
            "selection": {"order": "created_at ASC,id ASC"},
            "exposure_check": {"status": "incomplete", "unseen_not_proven": True},
            "taxonomy": {},
            "taxonomy_sha256": digest({}),
            "documents": documents,
        }
        pilot.atomic_json(
            self.folder / "snapshot.json",
            {"sha256": digest(snapshot), "snapshot": snapshot},
        )
        pilot.atomic_json(
            self.folder / "model-inputs.json",
            {"records": [document["model_input"] for document in documents]},
        )
        return documents

    def source_fixture(self):
        database = self.root / "source.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE curation_material (
                    id TEXT PRIMARY KEY, source_key TEXT, kind TEXT,
                    title TEXT, identifiers TEXT, created_at TEXT
                );
                CREATE TABLE curation_materialversion (
                    id TEXT PRIMARY KEY, material_id TEXT, media_type TEXT,
                    acquired_at TEXT, integrity_status TEXT, sha256 TEXT
                );
                CREATE TABLE curation_passage (
                    id TEXT PRIMARY KEY, version_id TEXT, parser_version TEXT,
                    ordinal INTEGER, locator TEXT, text TEXT
                );
                CREATE TABLE curation_literatureclassification (
                    version_id TEXT, taxonomy_version TEXT, result TEXT
                );
                """
            )
            # Reverse rowid order and tied creation times exercise both sort keys.
            for number in range(24, 0, -1):
                connection.execute(
                    "INSERT INTO curation_material VALUES (?,?,?,?,?,?)",
                    (
                        f"paper-{number:02}",
                        f"pubmed:{100000 + number}",
                        "paper",
                        "Unrelated genome reference"
                        if number == 1
                        else f"Cancer {number}",
                        json.dumps({"pubmed": str(100000 + number)}),
                        f"2026-10-{1 + (number - 1) // 3:02}T00:00:00+00:00",
                    ),
                )
            connection.execute(
                "INSERT INTO curation_material VALUES (?,?,?,?,?,?)",
                (
                    "guide",
                    "guide:test",
                    "guideline",
                    "Earlier guideline",
                    "{}",
                    "2020-01-01",
                ),
            )
            # The first paper has no full text. The 21st has full text but must
            # never replace it merely to make the experiment easier.
            connection.execute(
                "INSERT INTO curation_materialversion VALUES (?,?,?,?,?,?)",
                (
                    "fulltext-21",
                    "paper-21",
                    "application/pdf",
                    "2026-10-22",
                    "valid",
                    "a" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO curation_passage VALUES (?,?,?,?,?,?)",
                ("p21", "fulltext-21", "test-parser", 0, "page:1", "Drug response."),
            )
        return database

    def test_first_twenty_keep_unrelated_and_missing_fulltext_without_source_writes(
        self,
    ):
        database = self.source_fixture()
        before_bytes = database.read_bytes()
        before_mtime = database.stat().st_mtime_ns
        bundles, selection = pilot.read_source(database, 20)
        self.assertEqual(
            [bundle["material"]["id"] for bundle in bundles],
            [f"paper-{number:02}" for number in range(1, 21)],
        )
        self.assertEqual(bundles[0]["material"]["title"], "Unrelated genome reference")
        self.assertEqual(bundles[0]["versions"], [])
        self.assertEqual(bundles[0]["passages"], [])
        self.assertEqual(selection["scanned"], 25)
        self.assertEqual(database.read_bytes(), before_bytes)
        self.assertEqual(database.stat().st_mtime_ns, before_mtime)
        self.assertFalse(Path(str(database) + "-journal").exists())
        self.assertFalse(Path(str(database) + "-wal").exists())

    def test_selection_from_preserves_original_papers_after_earlier_record_is_added(
        self,
    ):
        database = self.source_fixture()
        bundles, selection = pilot.read_source(database, 20)
        expected_ids = [bundle["material"]["id"] for bundle in bundles]
        snapshot = {
            "database": str(database.resolve()),
            "limit": 20,
            "selection": selection,
            "documents": [
                {
                    "material_id": bundle["material"]["id"],
                    "created_at": bundle["material"]["created_at"],
                    "model_input": None,
                }
                for bundle in bundles
            ],
        }
        pilot.atomic_json(
            self.folder / "snapshot.json",
            {"sha256": digest(snapshot), "snapshot": snapshot},
        )
        pilot.atomic_json(self.folder / "model-inputs.json", {"records": []})
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO curation_material VALUES (?,?,?,?,?,?)",
                (
                    "earlier-record",
                    "pubmed:999999",
                    "paper",
                    "A newly imported old record",
                    '{"pubmed":"999999"}',
                    "1999-01-01",
                ),
            )
        newly_selected, _ = pilot.read_source(database, 20)
        self.assertEqual(newly_selected[0]["material"]["id"], "earlier-record")
        fixed, fixed_selection = pilot.read_source(
            database, 20, selection_from=self.folder
        )
        self.assertEqual([bundle["material"]["id"] for bundle in fixed], expected_ids)
        self.assertEqual(fixed_selection["prior_selection"], selection)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            pilot.read_source(database, 19, selection_from=self.folder)

    def test_targeted_selection_preserves_supplied_order_separate_from_first_twenty(
        self,
    ):
        database = self.source_fixture()
        selected_ids = ["paper-24", "paper-21", "paper-23"]
        targeted = {
            "selection_method": "targeted_solid_tumor_supplement",
            "records": [
                {
                    "material_id": material_id,
                    "created_at": (
                        "2026-10-07T00:00:00+00:00"
                        if material_id == "paper-21"
                        else "2026-10-08T00:00:00+00:00"
                    ),
                }
                for material_id in selected_ids
            ],
        }
        path = self.root / "targeted-selection.json"
        pilot.atomic_json(path, targeted)
        first_twenty, _ = pilot.read_source(database, 20)
        before = database.read_bytes()
        bundles, selection = pilot.read_source(database, 3, selection_file=path)
        self.assertEqual([row["material"]["id"] for row in bundles], selected_ids)
        self.assertTrue(
            set(selected_ids).isdisjoint(row["material"]["id"] for row in first_twenty)
        )
        self.assertEqual(selection["targeted_selection"], targeted)
        self.assertEqual(database.read_bytes(), before)
        targeted["records"][1] = targeted["records"][0].copy()
        pilot.atomic_json(path, targeted)
        with self.assertRaisesRegex(ValueError, "targeted selection"):
            pilot.read_source(database, 3, selection_file=path)

    def test_freeze_cannot_reuse_snapshot_for_different_targeted_selection(self):
        documents = self.write_snapshot()
        path = self.folder / "snapshot.json"
        envelope = json.loads(path.read_text())
        targeted = {
            "selection_method": "targeted_solid_tumor_supplement",
            "records": [
                {
                    "material_id": document["material_id"],
                    "created_at": document["created_at"],
                }
                for document in documents
            ],
        }
        envelope["snapshot"]["selection"]["targeted_selection"] = targeted
        envelope["sha256"] = digest(envelope["snapshot"])
        pilot.atomic_json(path, envelope)
        before = path.read_bytes()
        targeted["records"] = list(reversed(targeted["records"]))
        selection_file = self.root / "different-selection.json"
        pilot.atomic_json(selection_file, targeted)
        adapters = {
            "apps.curation.classification": SimpleNamespace(
                taxonomy_hash=digest, taxonomy_snapshot=dict
            )
        }
        with patch.dict(sys.modules, adapters), self.assertRaises(ValueError):
            pilot.freeze(
                envelope["snapshot"]["database"],
                envelope["snapshot"]["library"],
                self.folder,
                limit=3,
                selection_file=selection_file,
            )
        self.assertEqual(path.read_bytes(), before)

    def source_archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in entries:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return stream.getvalue()

    def test_worker_source_copy_verifies_hash_and_saves_only_requested_blob(self):
        content = b"A real-shaped source document for the offline fixture."
        sha = hashlib.sha256(content).hexdigest()
        name = f"blobs/{sha[:2]}/{sha}"
        version = {"sha256": sha, "integrity_status": "valid"}
        bundles = [{"versions": [version, version.copy()]}]
        completed = SimpleNamespace(stdout=self.source_archive([(name, content)]))
        with patch.object(pilot.subprocess, "run", return_value=completed) as command:
            library = pilot.copy_sources_via_worker(bundles, self.folder)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(json.loads(command.call_args.kwargs["input"]), [sha])
        self.assertEqual(pilot.verified_blob(library, version), content)
        self.assertEqual(
            [
                item.relative_to(library).as_posix()
                for item in library.rglob("*")
                if item.is_file()
            ],
            [name],
        )

    def test_worker_source_copy_rejects_traversal_duplicates_and_hash_mismatch(self):
        content = b"Expected immutable source bytes."
        sha = hashlib.sha256(content).hexdigest()
        name = f"blobs/{sha[:2]}/{sha}"
        bundles = [{"versions": [{"sha256": sha, "integrity_status": "valid"}]}]
        outside = self.root / sha
        outside.write_bytes(b"unchanged outside destination")
        cases = {
            "relative_traversal": [(f"../../{sha}", content)],
            "absolute_traversal": [(str(outside), content)],
            "duplicate": [(name, content), (name, content)],
            "hash_mismatch": [(name, b"Different source bytes")],
        }
        for case, entries in cases.items():
            output = self.folder / case
            with (
                self.subTest(case=case),
                patch.object(
                    pilot.subprocess,
                    "run",
                    return_value=SimpleNamespace(stdout=self.source_archive(entries)),
                ),
                self.assertRaises(ValueError),
            ):
                pilot.copy_sources_via_worker(bundles, output)
            self.assertEqual(outside.read_bytes(), b"unchanged outside destination")
            written = [path for path in output.rglob("*") if path.is_file()]
            if case == "duplicate":
                # The first valid member may already exist; the duplicate must
                # never write anything beyond that verified destination.
                self.assertEqual(written, [output / "source-library" / name])
                self.assertEqual(written[0].read_bytes(), content)
            else:
                self.assertEqual(written, [])

    def test_prepare_document_uses_pubmed_identifier_for_verified_metadata(self):
        content = b"<PubmedArticle><PMID>123456</PMID></PubmedArticle>"
        sha = hashlib.sha256(content).hexdigest()
        library = self.root / "library"
        blob = library / "blobs" / sha[:2] / sha
        blob.parent.mkdir(parents=True)
        blob.write_bytes(content)
        metadata = {
            "title": "Study of treatment response",
            "abstract": "Drug treatment showed no survival benefit.",
            "mesh": [],
            "publication_types": ["Journal Article"],
        }
        # Stub only Django-owned parsing/classification adapters. Blob integrity,
        # PMID comparison, input hashing and source preparation remain real.
        adapters = {
            "apps.curation.materials": SimpleNamespace(
                PARSER_VERSION="test-parser",
                pubmed_records=lambda root: [root],
                pubmed_record_id=lambda article: article.findtext("PMID"),
                pubmed_metadata=lambda article: metadata,
            ),
            "apps.curation.classification": SimpleNamespace(
                classify_metadata=lambda metadata, taxonomy: {"scope": "uncertain"}
            ),
            "apps.curation.network": SimpleNamespace(xml=ElementTree.fromstring),
        }
        bundle = {
            "material": {
                "id": "paper-1",
                "identifiers": {"pubmed": "123456"},
                "title": "Original material title",
                "created_at": "2026-10-15",
                "source_key": "pubmed:123456",
            },
            "versions": [
                {
                    "id": "version-1",
                    "media_type": "application/pubmed+xml",
                    "integrity_status": "valid",
                    "sha256": sha,
                }
            ],
            "passages": [],
            "stored_classifications": [],
        }
        with patch.dict(sys.modules, adapters):
            document = pilot.prepare_document(bundle, 1, library, {}, {})
        self.assertEqual(document["pmid"], "123456")
        self.assertEqual(document["model_input"]["pmid"], "123456")
        self.assertEqual(document["model_input"]["source_sha256"], sha)
        self.assertEqual(document["model_input"]["input_sha256"], digest(metadata))
        self.assertEqual(document["input_scope"], "abstract_only")
        self.assertEqual(document["passages"][0]["text"], metadata["abstract"])
        self.assertEqual(document["source_issues"], ["parsed_fulltext_missing"])

    def test_worker_source_copy_rejects_destination_symlink(self):
        content = b"Source bytes must stay in the experiment."
        sha = hashlib.sha256(content).hexdigest()
        name = f"blobs/{sha[:2]}/{sha}"
        outside = self.root / "outside"
        outside.mkdir()
        (self.folder / "source-library").symlink_to(outside, target_is_directory=True)
        bundles = [{"versions": [{"sha256": sha, "integrity_status": "valid"}]}]
        with (
            patch.object(
                pilot.subprocess,
                "run",
                return_value=SimpleNamespace(
                    stdout=self.source_archive([(name, content)])
                ),
            ),
            self.assertRaisesRegex(ValueError, "symlinks"),
        ):
            pilot.copy_sources_via_worker(bundles, self.folder)
        self.assertEqual(list(outside.iterdir()), [])

    def test_stop_pauses_between_documents_and_resume_reuses_saved_outputs(self):
        self.write_snapshot()

        def stop_after_first(document, **kwargs):
            result = locate_candidates(document, **kwargs)
            (self.folder / "STOP").touch()
            return result

        with patch.object(pilot, "locate_candidates", side_effect=stop_after_first):
            paused = pilot.run(self.folder)
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["processed"], 1)
        first_path = self.folder / "results" / "case-01.json"
        first_bytes = first_path.read_bytes()
        (self.folder / "STOP").unlink()
        with patch.object(
            pilot, "locate_candidates", wraps=locate_candidates
        ) as locate:
            resumed = pilot.run(self.folder)
        self.assertEqual(locate.call_count, 2)
        self.assertEqual(
            (resumed["state"], resumed["created"], resumed["reused"]),
            ("completed", 2, 1),
        )
        self.assertEqual(first_path.read_bytes(), first_bytes)
        with patch.object(
            pilot, "locate_candidates", side_effect=AssertionError("recomputed")
        ):
            repeated = pilot.run(self.folder)
        self.assertEqual((repeated["created"], repeated["reused"]), (0, 3))
        self.assertEqual(len(list((self.folder / "results").glob("case-*.json"))), 3)

    def test_interrupted_run_resumes_without_recomputing_completed_document(self):
        self.write_snapshot()
        calls = 0

        def interrupt_second(document, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated process failure")
            return locate_candidates(document, **kwargs)

        with (
            patch.object(pilot, "locate_candidates", side_effect=interrupt_second),
            self.assertRaisesRegex(RuntimeError, "simulated process failure"),
        ):
            pilot.run(self.folder)
        self.assertTrue((self.folder / "results" / "case-01.json").is_file())
        self.assertFalse((self.folder / "results" / "case-02.json").exists())
        resumed = pilot.run(self.folder)
        self.assertEqual((resumed["created"], resumed["reused"]), (2, 1))

    def test_snapshot_content_tampering_is_rejected(self):
        self.write_snapshot()
        path = self.folder / "snapshot.json"
        envelope = json.loads(path.read_text())
        envelope["snapshot"]["documents"][0]["title"] = "Changed after freeze"
        pilot.atomic_json(path, envelope)
        with self.assertRaisesRegex(ValueError, "snapshot"):
            pilot.run(self.folder)

    def test_model_input_tampering_is_rejected(self):
        self.write_snapshot()
        path = self.folder / "model-inputs.json"
        inputs = json.loads(path.read_text())
        inputs["records"][0]["metadata"]["title"] = "Changed input"
        pilot.atomic_json(path, inputs)
        with self.assertRaisesRegex(ValueError, "[Ii]nputs"):
            pilot.run(self.folder)

    def test_candidate_budget_change_cannot_reuse_previous_run(self):
        self.write_snapshot()
        pilot.run(self.folder, max_candidates=1)
        with self.assertRaisesRegex(ValueError, "configuration"):
            pilot.run(self.folder, max_candidates=2)

    def test_candidate_output_tampering_is_rejected_by_run_and_report(self):
        self.write_snapshot()
        pilot.run(self.folder)
        path = self.folder / "results" / "case-01.json"
        saved = json.loads(path.read_text())
        saved["result"]["candidate_passages"]["candidates"][0]["quote"] = (
            "Invented quote"
        )
        pilot.atomic_json(path, saved)
        for operation in (pilot.run, pilot.report):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(ValueError),
            ):
                operation(self.folder)

    def test_result_configuration_mismatch_is_rejected_by_run_and_report(self):
        self.write_snapshot()
        pilot.run(self.folder)
        path = self.folder / "results" / "case-01.json"
        saved = json.loads(path.read_text())
        saved["config_sha256"] = hashlib.sha256(b"other run configuration").hexdigest()
        pilot.atomic_json(path, saved)
        for operation in (pilot.run, pilot.report):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(ValueError),
            ):
                operation(self.folder)

    def test_report_keeps_invalid_and_unconfigured_results_out_of_knowledge_and_accuracy(
        self,
    ):
        documents = self.write_snapshot()
        pilot.run(self.folder)
        model_folder = self.folder / "model"
        model_folder.mkdir()
        pilot.atomic_json(
            model_folder / f"result-{documents[0]['pmid']}.json",
            {
                **documents[0]["model_input"],
                "status": "invalid_output",
                "decision": {"relevance": "solid", "scope": "solid_candidate"},
                "seconds": 0.25,
                "error": "not a validated decision",
            },
        )
        summary = pilot.report(self.folder)
        self.assertEqual(summary["documents"], 3)
        self.assertEqual(
            summary["model_statuses"], {"invalid_output": 1, "waiting_configuration": 2}
        )
        self.assertIsNone(summary["accuracy"])
        self.assertEqual(summary["published_evidence"], 0)
        self.assertFalse(summary["training"])
        comparison = json.loads((self.folder / "comparison.json").read_text())
        for row in comparison["records"]:
            self.assertEqual(row["model_relevance"], "")
            self.assertEqual(row["model_scope"], "")

    def test_report_rejects_model_result_from_another_source_version(self):
        documents = self.write_snapshot()
        model_folder = self.folder / "model"
        model_folder.mkdir()
        pilot.atomic_json(
            model_folder / f"result-{documents[0]['pmid']}.json",
            {
                **documents[0]["model_input"],
                "version_id": "another-source-version",
                "status": "invalid_output",
            },
        )
        with self.assertRaisesRegex(ValueError, "provenance"):
            pilot.report(self.folder)


if __name__ == "__main__":
    unittest.main()
