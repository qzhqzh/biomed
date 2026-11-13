import copy
import io
import json
import tempfile
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.curation.models import Concept, SourceRelease
from apps.curation.network import SourceBlocked, TransientSourceError, allowed, fetch
from apps.curation.standards import (
    ONCOTREE_DATE,
    ONCOTREE_VERSION,
    oncotree_terms,
    sync_standard,
)
from apps.curation.storage import read_blob


def node(code, parent, level, children=None):
    return {
        "code": code,
        "name": f"Source label {code}",
        "parent": parent,
        "level": level,
        "children": children or {},
        "externalReferences": {"NCI": ["C3512"], "UMLS": ["C0152013"]},
        "tissue": "Source tissue",
        "history": ["kept in original blob"],
    }


def tree(count=2):
    return {
        "TISSUE": node(
            "TISSUE",
            None,
            0,
            {f"C{i}": node(f"C{i}", "TISSUE", 1) for i in range(count)},
        )
    }


def metadata():
    return [{"api_identifier": ONCOTREE_VERSION, "release_date": ONCOTREE_DATE}]


class OncoTreeParsingTests(SimpleTestCase):
    def parse(self, value):
        with patch("apps.curation.standards.ONCOTREE_MIN_NODES", 3):
            return oncotree_terms(json.dumps(value).encode())

    def test_original_root_names_parent_and_reference_namespaces_preserved(self):
        terms = {item["id"]: item for item in self.parse(tree())}
        self.assertEqual(terms["ONCOTREE:TISSUE"]["parents"], [])
        child = terms["ONCOTREE:C0"]
        self.assertEqual(child["name"], "Source label C0")
        self.assertEqual(child["parents"], ["ONCOTREE:TISSUE"])
        self.assertEqual(child["xrefs"], ["NCI:C3512", "UMLS:C0152013"])
        self.assertEqual(child["synonyms"], [])

    def test_missing_root_parent_mismatch_and_invalid_level_rejected(self):
        for change in ("missing_root", "missing_parent", "level", "boolean_level"):
            value = tree()
            if change == "missing_root":
                value = value["TISSUE"]["children"]
            elif change == "missing_parent":
                value["TISSUE"]["children"]["C0"]["parent"] = "MISSING"
            else:
                value["TISSUE"]["children"]["C0"]["level"] = (
                    True if change == "boolean_level" else 9
                )
            with self.subTest(change=change), self.assertRaises(SourceBlocked):
                self.parse(value)

    def test_repeated_code_cycle_and_duplicate_json_key_rejected(self):
        value = tree()
        value["TISSUE"]["children"]["C0"]["children"]["TISSUE"] = node(
            "TISSUE", "C0", 2
        )
        with self.assertRaises(SourceBlocked):
            self.parse(value)
        with self.assertRaises(SourceBlocked):
            oncotree_terms(b'{"TISSUE":{},"TISSUE":{}}')

    def test_small_partial_snapshot_and_malformed_xrefs_rejected(self):
        with self.assertRaises(SourceBlocked):
            oncotree_terms(json.dumps(tree()).encode())
        value = tree()
        value["TISSUE"]["children"]["C0"]["externalReferences"] = {"NCI": "C3512"}
        with self.assertRaises(SourceBlocked):
            self.parse(value)


class OncoTreeSyncTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = override_settings(CURATION_STORAGE_DIR=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.old = SourceRelease.objects.create(
            namespace="ONCOTREE",
            version="old",
            sha256="a" * 64,
            count=1,
            active=True,
        )
        Concept.objects.create(release=self.old, identifier="ONCOTREE:OLD", label="Old")
        self.content = json.dumps(tree(700)).encode()

    def test_complete_snapshot_activates_and_raw_attributes_remain_available(self):
        with (
            patch(
                "apps.curation.standards.fetch_json", return_value=metadata()
            ) as versions,
            patch(
                "apps.curation.standards.fetch", return_value=self.content
            ) as download,
        ):
            result = sync_standard("ONCOTREE")
            again = sync_standard("ONCOTREE")
        release = SourceRelease.objects.get(pk=result["release_id"])
        self.assertEqual(result["release_id"], again["release_id"])
        self.assertEqual(release.count, 701)
        self.assertEqual(release.concepts.count(), 701)
        self.assertEqual(release.version, ONCOTREE_VERSION)
        self.assertEqual(read_blob(release.sha256), self.content)
        self.assertEqual(versions.call_count, 4)
        self.assertEqual(download.call_args.kwargs["bytes_per_second"], 256 * 1024)
        self.assertEqual(download.call_args.kwargs["interval"], 2)
        self.assertIn(f"version={ONCOTREE_VERSION}", download.call_args.args[0])
        self.old.refresh_from_db()
        self.assertFalse(self.old.active)
        self.assertEqual(self.old.concepts.count(), 1)

    def test_version_change_does_not_create_or_activate_snapshot(self):
        changed = copy.deepcopy(metadata())
        changed[0]["description"] = "changed while downloading"
        with (
            patch(
                "apps.curation.standards.fetch_json", side_effect=[metadata(), changed]
            ),
            patch("apps.curation.standards.fetch", return_value=self.content),
            self.assertRaises(TransientSourceError),
        ):
            sync_standard("ONCOTREE")
        self.assertEqual(SourceRelease.objects.count(), 1)
        self.assertTrue(SourceRelease.objects.get(pk=self.old.pk).active)

    def test_unknown_fixed_version_and_partial_response_preserve_old_release(self):
        for versions, content in (([], self.content), (metadata(), b"{}")):
            with (
                self.subTest(versions=versions),
                patch("apps.curation.standards.fetch_json", return_value=versions),
                patch("apps.curation.standards.fetch", return_value=content),
                self.assertRaises(SourceBlocked),
            ):
                sync_standard("ONCOTREE")
        self.assertEqual(SourceRelease.objects.count(), 1)
        self.assertTrue(SourceRelease.objects.get(pk=self.old.pk).active)

    def test_interrupted_batches_resume_without_publishing_partial_release(self):
        original = Concept.objects.bulk_create
        calls = 0

        def interrupted(rows, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Interrupted")
            return original(rows, **kwargs)

        with (
            patch("apps.curation.standards.fetch_json", return_value=metadata()),
            patch("apps.curation.standards.fetch", return_value=self.content),
        ):
            with (
                patch.object(Concept.objects, "bulk_create", side_effect=interrupted),
                self.assertRaises(RuntimeError),
            ):
                sync_standard("ONCOTREE")
            staged = SourceRelease.objects.exclude(pk=self.old.pk).get()
            self.assertEqual(staged.concepts.count(), 500)
            self.assertFalse(staged.active)
            self.assertTrue(SourceRelease.objects.get(pk=self.old.pk).active)
            sync_standard("ONCOTREE")
        staged.refresh_from_db()
        self.assertTrue(staged.active)
        self.assertEqual(staged.concepts.count(), 701)


class OncoTreeNetworkTests(SimpleTestCase):
    def test_exact_host_only(self):
        allowed("https://oncotree.mskcc.org/api/versions")
        for url in (
            "https://oncotree.mskcc.org.example.com/api/versions",
            "http://oncotree.mskcc.org/api/versions",
            "https://oncotree.mskcc.org:444/api/versions",
        ):
            with self.assertRaises(SourceBlocked):
                allowed(url)

    def test_throttled_reader_honours_rate_and_size_limit(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        def response(data):
            stream = io.BytesIO(data)
            stream.headers = {}
            return stream

        opener = MagicMock()
        opener.open.side_effect = [response(b"x" * 262144), response(b"x" * 11)]
        with (
            patch("apps.curation.network.build_opener", return_value=opener),
            patch("apps.curation.network.time.sleep", side_effect=sleep),
            patch("apps.curation.network.time.monotonic", side_effect=lambda: now[0]),
        ):
            result = fetch(
                "https://oncotree.mskcc.org/api/versions",
                bytes_per_second=262144,
                interval=2,
            )
            self.assertEqual(len(result), 262144)
            self.assertAlmostEqual(now[0], 3.0)
            with self.assertRaises(SourceBlocked):
                fetch(
                    "https://oncotree.mskcc.org/api/versions",
                    limit=10,
                    bytes_per_second=262144,
                )
