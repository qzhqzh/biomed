"""Offline integrity checks for reference registration."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import manage_reference_files as catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "catalog"
        self.source = self.directory / "annotation.gff.gz"
        self.source.write_bytes(b"reference fixture")
        self.entry = dict(source_path=str(self.source), relative_path="hg38/refseq/release-1/annotation.gff.gz",
                          provider="NCBI", dataset="RefSeq", assembly="GRCh38.p14", assembly_accession="GCF_000001405.40",
                          annotation_release="RS_2025_08", source_url="https://ftp.ncbi.nlm.nih.gov/example.gz",
                          retrieved_at=None, upstream_md5=hashlib.md5(self.source.read_bytes()).hexdigest())

    def register(self, *entries):
        return catalog.import_plan(self.root, {"entries": list(entries or [self.entry])})

    def test_reimport_preserves_provenance_and_reuses_file(self):
        self.assertEqual(self.register()["new_files"], 1)
        original = (self.root / "manifest.json").read_bytes()
        self.assertEqual(self.register()["new_files"], 0)
        duplicate = self.directory / "another-download.gz"
        duplicate.write_bytes(self.source.read_bytes())
        self.assertEqual(self.register({**self.entry, "source_path": str(duplicate)})["new_files"], 0)
        self.assertEqual((self.root / "manifest.json").read_bytes(), original)
        destination = self.root / self.entry["relative_path"]
        self.assertEqual(destination.stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(catalog.verify(self.root), {"verified_files": 1})

    def test_metadata_conflict_and_changed_content_cannot_overwrite(self):
        self.register()
        original = (self.root / "manifest.json").read_bytes()
        with self.assertRaisesRegex(catalog.CatalogError, "conflicts"):
            self.register({**self.entry, "annotation_release": "other-release"})
        alternative = self.directory / "other.gz"
        alternative.write_bytes(b"different annotation")
        with self.assertRaisesRegex(catalog.CatalogError, "conflicts"):
            self.register({**self.entry, "source_path": str(alternative), "upstream_md5": None})
        self.assertEqual((self.root / "manifest.json").read_bytes(), original)
        self.assertEqual((self.root / self.entry["relative_path"]).read_bytes(), b"reference fixture")

    def test_bad_md5_preflights_whole_plan_before_publishing(self):
        with self.assertRaisesRegex(catalog.CatalogError, "MD5 mismatch"):
            self.register(self.entry, {**self.entry, "relative_path": "hg19/bad.gz", "upstream_md5": "0" * 32})
        self.assertFalse((self.root / "manifest.json").exists())
        self.assertFalse((self.root / self.entry["relative_path"]).exists())

    def test_path_escape_and_symlink_are_rejected(self):
        for relative in (".", "../escape", "/escape", "hg38/../../escape", "manifest.json"):
            with self.subTest(relative=relative), self.assertRaises(catalog.CatalogError):
                self.register({**self.entry, "relative_path": relative})
        (self.root / "outside").symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(catalog.CatalogError):
            self.register({**self.entry, "relative_path": "outside/escape"})

    def test_identical_content_in_two_versions_uses_one_inode(self):
        self.register(self.entry, {**self.entry, "relative_path": "hg38/refseq/release-2/annotation.gff.gz", "annotation_release": "RS_2026_01"})
        manifest = json.loads((self.root / "manifest.json").read_text())
        paths = [self.root / key for key in manifest["files"]]
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0].stat().st_ino, paths[1].stat().st_ino)
        self.assertEqual(catalog.verify(self.root), {"verified_files": 2})

    def test_unknown_release_and_download_time_remain_null(self):
        self.register({**self.entry, "annotation_release": None, "retrieved_at": None})
        record = catalog.read_manifest(self.root)["files"][self.entry["relative_path"]]
        self.assertIsNone(record["annotation_release"])
        self.assertIsNone(record["retrieved_at"])


if __name__ == "__main__":
    unittest.main()
