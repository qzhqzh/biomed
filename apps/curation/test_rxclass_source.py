import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import sync_rxclass as sync

from . import rxclass_source as source
from .network import TransientSourceError


def classes():
    return {
        "rxclassMinConceptList": {
            "rxclassMinConcept": [
                {"classId": "A", "className": "One", "classType": "EPC"},
                {"classId": "B", "className": "Two", "classType": "EPC"},
            ]
        }
    }


def members(relation="DIRECT", tty="IN"):
    return {
        "drugMemberGroup": {
            "drugMember": [
                {
                    "minConcept": {"rxcui": "123", "name": "ingredient", "tty": tty},
                    "nodeAttr": [
                        {"attrName": "Relation", "attrValue": relation},
                        {"attrName": "SourceId", "attrValue": "source-123"},
                    ],
                }
            ]
        }
    }


class RxClassSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(source, "RESOURCE_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.calls = []
        self.sleeps = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == source.VERSION_URL:
            value = {"relaSourceVersion": "MEDRT 1"}
        elif url == source.RXNORM_VERSION_URL:
            value = {"relaSourceVersion": "RxNorm 1"}
        elif url == source.CLASSES_URL:
            value = classes()
        elif url == source.member_url("A"):
            value = members()
        elif url == source.member_url("B"):
            value = {"drugMemberGroup": {}}
        else:
            self.fail(url)
        return json.dumps(value).encode()

    def run_capture(self, fetcher=None):
        return source.fetch_snapshot(
            self.root / "snapshot",
            fetcher=fetcher or self.fetch,
            sleeper=self.sleeps.append,
        )

    def test_complete_snapshot_and_immutable_repeat(self):
        result = self.run_capture()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["counts"]["classes_completed"], 2)
        self.assertEqual(result["counts"]["memberships"], 1)
        self.assertEqual(result["counts"]["ingredients"], 1)
        self.assertEqual(result["empty_classes"], ["B"])
        for _, options in self.calls:
            self.assertEqual(options["interval"], 2)
            self.assertEqual(options["bytes_per_second"], 262144)
        count = len(self.calls)
        before = (self.root / "snapshot/manifest.json").read_bytes()
        self.run_capture()
        self.assertEqual(len(self.calls), count)
        self.assertEqual((self.root / "snapshot/manifest.json").read_bytes(), before)
        rows = json.loads((self.root / "snapshot/memberships.json").read_text())
        self.assertEqual(
            rows[0]["raw_members"][0]["nodeAttr"][1]["attrValue"], "source-123"
        )

    def test_failure_does_not_publish_empty_results_and_resumes(self):
        def fail_b(url, **kwargs):
            if url == source.member_url("B"):
                raise TransientSourceError("temporary")
            return self.fetch(url, **kwargs)

        with self.assertRaises(TransientSourceError):
            self.run_capture(fail_b)
        self.assertEqual(self.sleeps, [5, 15, 45])
        self.assertFalse((self.root / "snapshot/memberships.json").exists())
        paused = json.loads((self.root / "snapshot/manifest.json").read_text())
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(len(paused["failures"]), 4)
        self.run_capture()
        self.assertEqual(sum(url == source.member_url("A") for url, _ in self.calls), 1)

    def test_version_changes_during_capture_rejected(self):
        calls = 0

        def changed(url, **kwargs):
            nonlocal calls
            if url == source.VERSION_URL:
                calls += 1
                return json.dumps({"relaSourceVersion": f"MEDRT {calls}"}).encode()
            return self.fetch(url, **kwargs)

        with self.assertRaisesRegex(source.SnapshotError, "version changed"):
            self.run_capture(changed)
        self.assertFalse((self.root / "snapshot/memberships.json").exists())

    def test_changed_version_cannot_mix_with_resume_cache(self):
        def fail_b(url, **kwargs):
            if url == source.member_url("B"):
                raise TransientSourceError("temporary")
            return self.fetch(url, **kwargs)

        with self.assertRaises(TransientSourceError):
            self.run_capture(fail_b)

        def changed(url, **kwargs):
            if url == source.VERSION_URL:
                return b'{"relaSourceVersion":"MEDRT 2"}'
            return self.fetch(url, **kwargs)

        with self.assertRaisesRegex(source.SnapshotError, "version changed"):
            self.run_capture(changed)
        self.assertFalse((self.root / "snapshot/memberships.json").exists())

    def test_changed_class_list_prevents_publication(self):
        calls = 0

        def changed(url, **kwargs):
            nonlocal calls
            if url == source.CLASSES_URL:
                calls += 1
                value = classes()
                if calls > 1:
                    value["rxclassMinConceptList"]["rxclassMinConcept"][0][
                        "className"
                    ] = "Changed"
                return json.dumps(value).encode()
            return self.fetch(url, **kwargs)

        with self.assertRaisesRegex(source.SnapshotError, "class list changed"):
            self.run_capture(changed)
        self.assertFalse((self.root / "snapshot/memberships.json").exists())

    def test_malformed_response_is_retained_but_never_cached_as_success(self):
        def malformed(url, **kwargs):
            if url == source.member_url("B"):
                return b'{"error":"temporary API error"}'
            return self.fetch(url, **kwargs)

        with self.assertRaises(source.SnapshotError):
            self.run_capture(malformed)
        manifest = json.loads((self.root / "snapshot/manifest.json").read_text())
        self.assertEqual(manifest["requests"][-1]["state"], "invalid")
        self.assertTrue(
            (self.root / "snapshot" / manifest["requests"][-1]["raw_file"]).exists()
        )
        self.assertFalse((self.root / "snapshot/memberships.json").exists())
        self.assertEqual(self.run_capture()["state"], "completed")

    def test_empty_document_valid_but_errors_never_count_empty(self):
        self.assertEqual(source.parse_memberships({}, "A", "url", "sha"), [])
        for invalid in ([], {"error": "service unavailable"}, {"drugMemberGroup": []}):
            with self.assertRaises(source.SnapshotError):
                source.parse_memberships(invalid, "A", "url", "sha")

    def test_indirect_non_ingredient_and_duplicate_classes_rejected(self):
        for payload in (members("INDIRECT"), members(tty="PIN")):
            with self.assertRaises(source.SnapshotError):
                source.parse_memberships(payload, "A", "url", "sha")
        payload = classes()
        payload["rxclassMinConceptList"]["rxclassMinConcept"].append(
            payload["rxclassMinConceptList"]["rxclassMinConcept"][0]
        )
        with self.assertRaises(source.SnapshotError):
            source.parse_classes(payload)

    def test_duplicate_members_preserve_all_original_source_assertions(self):
        payload = members()
        payload["drugMemberGroup"]["drugMember"] *= 2
        rows = source.parse_memberships(payload, "A", "url", "sha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["raw_members"]), 2)

    def test_corrupted_cache_is_rejected(self):
        result = self.run_capture()
        (self.root / "snapshot" / result["requests"][0]["raw_file"]).write_bytes(b"{}")
        with self.assertRaisesRegex(source.SnapshotError, "checksum mismatch"):
            self.run_capture()

    def test_concurrent_capture_and_outside_resource_directory_blocked(self):
        directory = self.root / "snapshot"
        directory.mkdir()
        with (directory / ".capture.lock").open("a+") as lock:
            source.fcntl.flock(
                lock.fileno(), source.fcntl.LOCK_EX | source.fcntl.LOCK_NB
            )
            with self.assertRaisesRegex(source.SnapshotError, "active capture"):
                self.run_capture()
        with self.assertRaisesRegex(source.SnapshotError, "must be inside"):
            source.fetch_snapshot(self.root.parent / "outside", fetcher=self.fetch)


class RxClassSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(source, "RESOURCE_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.database = self.root / "existing.sqlite3"
        self.database.touch()
        self.library = self.root / "library"
        self.library.mkdir()
        self.output = self.root / "snapshot"
        self.sleeps = []
        self.manifest = {
            "state": "completed",
            "version": "MEDRT test",
            "counts": {"classes_total": 2, "classes_completed": 2, "memberships": 3},
        }
        self.receipt = {
            "id": "dataset-id",
            "namespace": "RXCLASS_EPC",
            "version": "MEDRT test",
            "active": True,
            "counts": self.manifest["counts"].copy(),
        }
        self.runner = Mock(
            return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(self.receipt), stderr=""
            )
        )

    def fetch(self, directory):
        source.atomic_json(directory / "manifest.json", self.manifest)
        return self.manifest

    def run_sync(self, fetcher=None):
        return sync.sync_snapshot(
            self.output,
            self.database,
            self.library,
            fetcher=fetcher or self.fetch,
            runner=self.runner,
            sleeper=self.sleeps.append,
        )

    def test_complete_requires_real_successful_import_receipt(self):
        status = self.run_sync()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["import_result"], self.receipt)
        self.assertEqual(self.runner.call_count, 1)
        arguments, options = self.runner.call_args
        self.assertIn("import_reference_resources", arguments[0])
        self.assertEqual(options["env"]["DATABASE_PATH"], str(self.database))
        self.assertEqual(options["env"]["CURATION_STORAGE_DIR"], str(self.library))
        self.run_sync()
        self.assertEqual(self.runner.call_count, 1)

    def test_transient_capture_retries_are_bounded_per_checkpoint(self):
        fetcher = Mock(side_effect=TransientSourceError("temporary"))
        status = self.run_sync(fetcher)
        self.assertEqual(status["state"], "paused")
        self.assertEqual(fetcher.call_count, 4)
        self.assertEqual(sum(self.sleeps), 1260)
        self.runner.assert_not_called()

    def test_version_integrity_error_pauses_without_retry_or_import(self):
        status = self.run_sync(
            Mock(side_effect=source.SnapshotError("version changed"))
        )
        self.assertEqual(status["state"], "paused")
        self.assertFalse(self.sleeps)
        self.runner.assert_not_called()

    def test_completed_source_is_not_completed_sync_if_import_fails(self):
        self.runner.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr="CommandError: checksum mismatch"
        )
        status = self.run_sync()
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["phase"], "import")
        self.assertEqual(self.runner.call_count, 1)
        self.assertFalse(self.sleeps)

    def test_transient_database_lock_retries_same_full_snapshot(self):
        self.runner.side_effect = [
            SimpleNamespace(
                returncode=1, stdout="", stderr="OperationalError: database is locked"
            ),
            self.runner.return_value,
        ]
        status = self.run_sync()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.sleeps, [15])

    def test_bad_import_receipt_never_marks_completed(self):
        self.receipt["counts"]["memberships"] = 2
        self.runner.return_value.stdout = json.dumps(self.receipt)
        status = self.run_sync()
        self.assertEqual(status["state"], "paused")
        self.assertIn("counts differ", status["error"])

    def test_target_rebinding_and_concurrent_sync_rejected(self):
        self.run_sync()
        other = self.root / "other.sqlite3"
        other.touch()
        with self.assertRaisesRegex(source.SnapshotError, "different target"):
            sync.sync_snapshot(self.output, other, self.library)
        with (self.output / ".sync.lock").open("a+") as lock:
            source.fcntl.flock(
                lock.fileno(), source.fcntl.LOCK_EX | source.fcntl.LOCK_NB
            )
            with self.assertRaisesRegex(source.SnapshotError, "sync is active"):
                self.run_sync()


if __name__ == "__main__":
    unittest.main()
