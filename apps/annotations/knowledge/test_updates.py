import gzip
import hashlib
import multiprocessing
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, SimpleTestCase, override_settings

from apps.annotations.knowledge import store, updater
from apps.annotations.knowledge.releases import active_dir


GENE = ("#tax_id\tGeneID\tSymbol\tdescription\ttype_of_gene\tdbXrefs\tSynonyms\n"
        "9606\t7157\tTP53\ttumor protein p53\tprotein-coding\t-\tP53\n")
MANE = ("#NCBI_GeneID\tsymbol\tRefSeq_nuc\tEnsembl_nuc\tRefSeq_prot\tEnsembl_prot\tMANE_status\n"
        "GeneID:7157\tTP53\tNM_000546.6\tENST00000269305.9\tNP_000537.3\tENSP00000269305.4\tMANE Select\n")
README = "MANE Version\t1.5\nNCBI RefSeq Annotation Release\tRS_test\nEnsembl Release\t116\n"


class KnowledgeUpdateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "baseline"
        self.updates = Path(self.temp.name) / "updates"
        (self.base / "raw").mkdir(parents=True)
        self.settings_override = override_settings(KNOWLEDGE_DIR=str(self.base), KNOWLEDGE_UPDATES_DIR=str(self.updates))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.minimum = patch.object(updater, "MIN_ROWS", 1)
        self.minimum.start()
        self.addCleanup(self.minimum.stop)
        self.gene, self.mane, self.readme = GENE, MANE, README
        for key, text in (("gene_info", GENE), ("mane", MANE)):
            (self.base / "raw" / updater.RAW_NAMES[key]).write_bytes(gzip.compress(text.encode(), mtime=1))
        genes, transcripts = updater.build_candidate(self.base, {})
        updater.write_json(self.base / "meta.json", {"imported_at": "2026-01-01T00:00:00Z",
                           "genes": genes, "mane": {"version": "1.5", **transcripts}})
        self.network = patch.object(updater, "download", side_effect=self.download)
        self.network.start()
        self.addCleanup(self.network.stop)

    def download(self, url, destination, max_bytes=None):
        if url.endswith("README_versions.txt"):
            data = self.readme.encode()
        else:
            data = gzip.compress((self.gene if "gene_info" in url else self.mane).encode(), mtime=2)
        destination.write_bytes(data)
        return {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "last_modified": "Thu, 10 Sep 2026 00:00:00 GMT"}

    def run_update(self, action="update"):
        return updater.start_update(action, background=False)

    def test_recompression_does_not_publish_or_change_local_date(self):
        result = self.run_update()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["published"])
        self.assertFalse(result["sources"]["gene_info"]["changed"])
        self.assertEqual(active_dir(), self.base)
        self.assertEqual(updater.get_status()["current"]["imported_at"], "2026-01-01T00:00:00Z")
        self.assertTrue(result["last_checked_at"])

    def test_check_only_and_atomic_update_then_idempotence(self):
        self.gene = GENE.replace("tumor protein p53", "updated name")
        result = self.run_update("check")
        self.assertTrue(result["update_available"])
        self.assertFalse((self.updates / "current.json").exists())
        store.reload()
        with store.pin_snapshot():
            result = self.run_update()
            self.assertTrue(result["published"])
            self.assertEqual(store.search_genes("TP53")["results"][0]["name"], "tumor protein p53")
        self.assertEqual(store.search_genes("TP53")["results"][0]["name"], "updated name")
        meta = updater.get_status()["current"]
        self.assertEqual(meta["updates"]["sources"]["mane"]["last_updated_at"], "2026-01-01T00:00:00Z")
        published_dir = active_dir()
        self.assertEqual(published_dir.stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.base / "genes.jsonl").is_file())
        self.assertFalse(self.run_update()["published"])
        self.assertEqual(active_dir(), published_dir)
        self.assertEqual(updater.get_status()["current"]["imported_at"], meta["imported_at"])

    def test_version_is_discovered_and_published(self):
        self.readme = README.replace("1.5", "1.6")
        result = self.run_update()
        self.assertTrue(result["published"])
        self.assertIn("v1.6.summary", result["sources"]["mane"]["url"])
        self.assertEqual(updater.get_status()["current"]["mane"]["version"], "1.6")

    def test_alias_indexes_stay_with_pinned_snapshot_after_reload(self):
        store.reload()
        self.gene = GENE.replace("TP53", "NEW53")
        self.mane = MANE.replace("TP53", "NEW53")
        with store.pin_snapshot():
            self.assertTrue(self.run_update()["published"])
            store.reload()
            self.assertEqual(store.lookup_transcript("nm000546.6")["gene"], "TP53")
            self.assertEqual(store.search_transcripts("NM000546.6")["results"][0]["gene"], "TP53")
            result = store.search_genes("NM_000546.999")
            self.assertEqual(result["results"][0]["symbol"], "TP53")
            self.assertFalse(result["query_resolution"]["version_checked"])
        self.assertEqual(store.lookup_transcript("nm000546.6")["gene"], "NEW53")
        self.assertEqual(store.search_genes("NM_000546.999")["results"][0]["symbol"], "NEW53")

    def test_failure_keeps_published_snapshot_and_last_successful_check(self):
        self.gene = GENE.replace("tumor protein p53", "updated name")
        successful = self.run_update()
        current = (self.updates / "current.json").read_bytes()
        self.gene = "broken header\n"
        with self.assertLogs(updater.logger, level="ERROR"):
            result = self.run_update()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_checked_at"], successful["last_checked_at"])
        self.assertEqual((self.updates / "current.json").read_bytes(), current)

    def test_corrupt_download_and_wrong_species_are_rejected(self):
        for corrupt in (True, False):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    def broken(url, destination, *args):
                        if url.endswith(".gz"):
                            destination.write_bytes(b"not gzip")
                            return {"url": url}
                        return self.download(url, destination)
                    mock = patch.object(updater, "download", side_effect=broken)
                else:
                    self.gene = GENE.replace("9606", "10090")
                    mock = patch.object(updater, "download", side_effect=self.download)
                with mock, self.assertLogs(updater.logger, level="ERROR"):
                    self.assertEqual(self.run_update()["status"], "failed")
                self.assertEqual(active_dir(), self.base)

    def test_count_drop_and_duplicate_rows_are_rejected(self):
        meta = updater.read_json(self.base / "meta.json")
        meta["genes"]["gene_count"] = 100
        updater.write_json(self.base / "meta.json", meta)
        with self.assertLogs(updater.logger, level="ERROR"):
            self.assertEqual(self.run_update()["status"], "failed")
        self.gene += GENE.splitlines(keepends=True)[1]
        with self.assertLogs(updater.logger, level="ERROR"):
            self.assertIn("重复主键", self.run_update()["message"])

    def test_release_race_is_rejected(self):
        calls = 0
        def moving(url, destination, *args):
            nonlocal calls
            if url.endswith("README_versions.txt"):
                calls += 1
                if calls == 2:
                    self.readme = README.replace("1.5", "1.6")
            return self.download(url, destination)
        with patch.object(updater, "download", side_effect=moving), self.assertLogs(updater.logger, level="ERROR"):
            self.assertIn("切换版本", self.run_update()["message"])
        self.assertEqual(active_dir(), self.base)

    def test_lock_and_interrupted_recovery(self):
        lock = updater.acquire_lock()
        try:
            updater.write_json(self.updates / "status.json", {"status": "running"})
            self.assertEqual(updater.get_status()["status"], "running")
            with self.assertRaises(updater.UpdateBusy):
                self.run_update()
        finally:
            lock.close()
        self.assertEqual(updater.get_status()["status"], "interrupted")
        self.assertEqual(self.run_update()["status"], "succeeded")

    def test_missing_local_files_require_rebuild(self):
        (self.base / "transcript-map.json").unlink()
        self.assertTrue(self.run_update()["published"])
        self.assertTrue((active_dir() / "transcript-map.json").is_file())

    def test_another_process_reloads_after_publish(self):
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()
        def reader():
            try:
                child.send(store.search_genes("TP53")["results"][0]["name"])
                child.recv()
                child.send(store.search_genes("TP53")["results"][0]["name"])
            finally:
                child.close()
        process = context.Process(target=reader)
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "tumor protein p53")
            self.gene = GENE.replace("tumor protein p53", "new version")
            self.assertTrue(self.run_update()["published"])
            parent.send("published")
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "new version")
        finally:
            parent.close()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()

    def test_pointer_write_failure_preserves_previous_data(self):
        self.gene = GENE.replace("tumor protein p53", "new version")
        write = updater.write_json
        def fail_pointer(path, value):
            if path.name == "current.json":
                raise OSError("simulated disk write failure")
            write(path, value)
        with patch.object(updater, "write_json", side_effect=fail_pointer), self.assertLogs(updater.logger, level="ERROR"):
            self.assertEqual(self.run_update()["status"], "failed")
        self.assertEqual(active_dir(), self.base)

    def test_non_coding_mane_has_no_dash_protein_key(self):
        self.mane = MANE.replace("NM_000546.6", "NR_000546.6").replace("NP_000537.3", "-").replace("ENSP00000269305.4", "-")
        self.assertTrue(self.run_update()["published"])
        records = updater.read_json(active_dir() / "transcript-map.json")["records"]
        self.assertNotIn("-", records)
        self.assertIn("NR_000546", records)

    def test_official_human_subtaxa_are_preserved(self):
        self.gene += "63221\t6775062\ttrnT\tTRNA\ttRNA\t-\t-\n"
        self.gene += "741158\t6775063\tND6\tND6\tprotein-coding\t-\t-\n"
        self.assertTrue(self.run_update()["published"])
        self.assertEqual(updater.get_status()["current"]["genes"]["gene_count"], 3)

    def test_non_official_redirects_and_path_traversal_are_rejected(self):
        for url in ("http://ftp.ncbi.nlm.nih.gov/test", "https://evil.test/file", "https://ftp.ncbi.nlm.nih.gov@evil.test/file"):
            with self.assertRaises(ValueError):
                updater.check_url(url)
        self.updates.mkdir()
        updater.write_json(self.updates / "current.json", {"release": "../baseline"})
        with self.assertRaises(ValueError):
            active_dir()

    @override_settings(BIOTOOLS_API_KEYS="", BIOTOOLS_REQUIRE_API_KEY=False)
    def test_api_csrf_invalid_action_and_busy(self):
        client = Client(enforce_csrf_checks=True)
        endpoint = "/api/v1/knowledge/updates/"
        self.assertEqual(client.get(endpoint).status_code, 200)
        self.assertEqual(client.post(endpoint, {"action": "update"}, content_type="application/json").status_code, 403)
        page = client.get("/tools/data-updates/")
        self.assertContains(page, 'type="module"')
        self.assertRegex(page.content.decode(), r"portal/dist/assets/biomed-[A-Za-z0-9_-]+\.js")
        self.assertRegex(page.content.decode(), r"portal/dist/assets/index-[A-Za-z0-9_-]+\.css")
        token = client.cookies["csrftoken"].value
        headers = {"HTTP_X_CSRFTOKEN": token}
        for invalid in ("bad", [], {}, None, 1):
            self.assertEqual(client.post(endpoint, {"action": invalid}, content_type="application/json", **headers).status_code, 400)
        lock = updater.acquire_lock()
        try:
            self.assertEqual(client.post(endpoint, {"action": "check"}, content_type="application/json", **headers).status_code, 409)
        finally:
            lock.close()
        with patch.object(updater, "start_update", return_value={"status": "running"}) as start:
            self.assertEqual(client.post(endpoint, {"action": "check"}, content_type="application/json", **headers).status_code, 202)
            start.assert_called_once_with("check")

    @override_settings(BIOTOOLS_API_KEYS="test-only-key", BIOTOOLS_REQUIRE_API_KEY=True)
    def test_api_requires_configured_key(self):
        endpoint = "/api/v1/knowledge/updates/"
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.get(endpoint, HTTP_X_API_KEY="test-only-key").status_code, 200)
