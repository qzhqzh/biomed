"""Offline coverage for bounded transcript retrieval and exact-version resume."""

import gzip
import io
import json
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from scripts.fetch_transcript_exons import (
    ENDPOINT,
    Downloader,
    FetchError,
    OfficialRedirect,
    fetch_batches,
    load_accessions,
    validate_response,
)


def record(accession, organism="Homo sapiens"):
    return (
        f"LOCUS       {accession.split('.')[0]} 6 bp mRNA linear PRI 01-JAN-2026\n"
        f"ACCESSION   {accession.split('.')[0]}\nVERSION     {accession}\n"
        f"  ORGANISM  {organism}\n"
        "FEATURES             Location/Qualifiers\n"
        "     source          1..6\n"
        f'                     /organism="{organism}"\n'
        "     gene            1..6\n"
        '                     /gene="TEST"\n'
        "     exon            1..3\n"
        "     exon            4..6\n"
        "ORIGIN\n        1 acgtac\n//\n"
    ).encode()


class Clock:
    def __init__(self):
        self.value = 0.0
        self.delays = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


class Response(io.BytesIO):
    def __init__(self, body, url=ENDPOINT, length=True):
        super().__init__(body)
        self.url = url
        self.headers = {"Content-Length": str(len(body))} if length else {}

    def geturl(self):
        return self.url


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.clock = Clock()

    def opener(self, request, timeout):
        ids = parse_qs(urlsplit(request.full_url).query)["id"][0].split(",")
        self.calls.append(ids)
        return Response(b"".join(record(accession) for accession in ids), request.full_url)

    def downloader(self, **kwargs):
        return Downloader(max_bytes=kwargs.pop("max_bytes", 1024 * 1024), opener=kwargs.pop("opener", self.opener),
                          sleep=self.clock.sleep, monotonic=self.clock.monotonic, **kwargs)

    def test_record_budget_resume_reuses_hash_checked_batches(self):
        ids = ["NM_1.1", "NM_2.1", "NR_3.1"]
        first = fetch_batches(ids, self.root, max_records=2, downloader=self.downloader())
        self.assertEqual((first["state"], first["new_records"], first["pending"]), ("paused_record_budget", 2, 1))
        second = fetch_batches(ids, self.root, max_records=1, downloader=self.downloader())
        self.assertEqual((second["state"], second["cache_reused"], second["new_records"]), ("complete", 2, 1))
        self.assertEqual(self.calls, [ids[:2], ids[2:]])
        third = fetch_batches(ids, self.root, downloader=self.downloader())
        self.assertEqual(third["requests"], 0)
        self.assertEqual(len(list(self.root.glob("*.gb"))), 2)
        manifest = json.loads((self.root / "sources.json").read_text())
        for item in manifest["records"]:
            payload = Path(item["path"]).read_bytes()[item["byte_start"]:item["byte_end"]]
            self.assertIn(f'VERSION     {item["accession"]}'.encode(), payload)

    def test_connection_failure_pauses_then_resume_never_repeats_completed_batch(self):
        ids = ["NM_1.1", "NM_2.1", "NM_3.1"]

        def interrupted(request, timeout):
            requested = parse_qs(urlsplit(request.full_url).query)["id"][0].split(",")
            if "NM_3.1" in requested:
                self.calls.append(requested)
                raise URLError("temporary network problem")
            return self.opener(request, timeout)

        first = fetch_batches(ids, self.root, batch_size=2, downloader=self.downloader(opener=interrupted))
        self.assertEqual(first["state"], "paused_error")
        self.assertEqual((first["completed"], first["failed"], first["requests"]), (2, 1, 5))
        self.assertFalse(first["complete"])
        self.calls.clear()
        result = fetch_batches(ids, self.root, downloader=self.downloader())
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.calls, [["NM_3.1"]])

    def test_wrong_version_rejected_without_persisting_batch(self):
        client = self.downloader(opener=lambda request, timeout: Response(record("NM_1.2")))
        result = fetch_batches(["NM_1.1"], self.root, downloader=client)
        self.assertEqual(result["state"], "paused_error")
        self.assertEqual((result["completed"], result["pending"]), (0, 1))
        manifest = json.loads((self.root / "sources.json").read_text())
        self.assertIn("missing=['NM_1.1']", manifest["errors"][0]["error"])
        self.assertEqual(list(self.root.glob("*.gb")), [])

    def test_missing_duplicate_nonhuman_and_truncated_records_rejected(self):
        for payload, ids in [
            (record("NM_1.1"), ["NM_1.1", "NM_2.1"]),
            (record("NM_1.1") * 2, ["NM_1.1"]),
            (record("NM_1.1", organism="Mus musculus"), ["NM_1.1"]),
            (record("NM_1.1")[:-3], ["NM_1.1"]),
        ]:
            with self.subTest(ids=ids, size=len(payload)), self.assertRaises(FetchError):
                validate_response(payload, ids)

    def test_cache_tampering_is_not_treated_as_a_hit(self):
        fetch_batches(["NM_1.1"], self.root, downloader=self.downloader())
        next(self.root.glob("*.gb")).write_bytes(record("NM_1.2"))
        self.calls.clear()
        with self.assertRaisesRegex(FetchError, "hash/size mismatch"):
            fetch_batches(["NM_1.1"], self.root, downloader=self.downloader())
        self.assertEqual(self.calls, [])

    def test_external_single_record_manifest_is_reused(self):
        legacy = self.root / "legacy"
        result = fetch_batches(["NM_1.1"], legacy, downloader=self.downloader())
        self.assertTrue(result["complete"])
        manifest = json.loads((legacy / "sources.json").read_text())
        source = manifest["sources"][0]
        source["accession"] = source.pop("accessions")[0]
        (legacy / "sources.json").write_text(json.dumps({"sources": [source]}))
        self.calls.clear()
        result = fetch_batches(["NM_1.1", "NM_2.1"], self.root / "next", reuse_manifests=[legacy / "sources.json"], downloader=self.downloader())
        self.assertEqual((result["cache_reused"], result["new_records"]), (1, 1))
        self.assertEqual(self.calls, [["NM_2.1"]])

    def test_http_retry_after_and_request_interval_are_respected(self):
        starts = []

        def throttled(request, timeout):
            starts.append(self.clock.value)
            if len(starts) == 1:
                raise HTTPError(request.full_url, 429, "throttle", {"Retry-After": "12"}, None)
            return self.opener(request, timeout)

        client = self.downloader(opener=throttled, bytes_per_second=100)
        client.fetch(["NM_1.1"])
        client.fetch(["NM_2.1"])
        self.assertGreaterEqual(starts[1] - starts[0], 12)
        self.assertGreaterEqual(starts[2] - starts[1], 2)
        self.assertGreaterEqual(self.clock.value - 12 + 1e-9, client.bytes / 100)

    def test_incomplete_read_retries_and_accounts_partial_bytes(self):
        calls = []

        class Truncated(Response):
            def read(self, size=-1):
                raise IncompleteRead(b"LOCUS", 50)

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                return Truncated(b"ignored")
            return Response(record("NM_1.1"))

        result = fetch_batches(["NM_1.1"], self.root, downloader=self.downloader(opener=opener))
        self.assertEqual((result["state"], result["requests"]), ("complete", 2))
        self.assertEqual(result["network_response_bytes"], len(record("NM_1.1")) + 5)

    def test_extreme_retry_after_pauses_without_sleeping_forever(self):
        for value in ("inf", "nan", "999999999"):
            def opener(request, timeout, retry_value=value):
                raise HTTPError(request.full_url, 429, "throttle", {"Retry-After": retry_value}, None)

            with self.subTest(value=value):
                result = fetch_batches(["NM_1.1"], self.root / value, downloader=self.downloader(opener=opener))
                self.assertEqual((result["state"], result["requests"]), ("paused_error", 1))

    def test_relocated_legacy_path_is_checked_by_basename_and_hash(self):
        legacy = self.root / "legacy"
        fetch_batches(["NM_1.1"], legacy, downloader=self.downloader())
        manifest = json.loads((legacy / "sources.json").read_text())
        source = manifest["sources"][0]
        source["path"] = str(self.root / "moved-away" / Path(source["path"]).name)
        (legacy / "sources.json").write_text(json.dumps(manifest))
        self.calls.clear()
        result = fetch_batches(["NM_1.1"], self.root / "next", reuse_manifests=[legacy / "sources.json"], downloader=self.downloader())
        self.assertEqual((result["state"], result["cache_reused"], result["requests"]), ("complete", 1, 0))

    def test_strict_byte_budget_and_exact_content_length_boundary(self):
        body = record("NM_1.1")
        client = self.downloader(max_bytes=len(body))
        result = fetch_batches(["NM_1.1"], self.root / "exact", max_bytes=len(body), downloader=client)
        self.assertEqual((result["state"], client.bytes), ("complete", len(body)))
        client = self.downloader(max_bytes=len(body) - 1)
        result = fetch_batches(["NM_1.1"], self.root / "small", max_bytes=len(body) - 1, downloader=client)
        self.assertEqual(result["state"], "paused_byte_budget")
        self.assertLessEqual(client.bytes, len(body) - 1)
        self.assertFalse(result["complete"])

    def test_stream_without_length_never_overreads_byte_budget(self):
        body = record("NM_1.1")
        client = self.downloader(max_bytes=20, opener=lambda request, timeout: Response(body, length=False))
        result = fetch_batches(["NM_1.1"], self.root, downloader=client)
        self.assertEqual((result["state"], client.bytes), ("paused_byte_budget", 20))
        self.assertEqual(list(self.root.glob("*.gb")), [])

    def test_input_formats_and_safety_bounds(self):
        path = self.root / "ids.tsv"
        path.write_text("accession\tgene\nNM_1.1\tTEST\nNM_1.1\tTEST\nNR_2.1\tTEST\n")
        self.assertEqual(load_accessions(path), ["NM_1.1", "NR_2.1"])
        mane = self.root / "mane.txt.gz"
        with gzip.open(mane, "wt") as handle:
            handle.write("#NCBI_GeneID\tRefSeq_nuc\nGeneID:1\tNM_1.1\n")
        self.assertEqual(load_accessions(mane, mane=True), ["NM_1.1"])
        path.write_text("NM_1\n")
        with self.assertRaises(FetchError):
            load_accessions(path)
        for kwargs in ({"interval": 1.99}, {"bytes_per_second": 262145}, {"max_bytes": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(FetchError):
                self.downloader(**kwargs)
        with self.assertRaises(FetchError):
            fetch_batches(["NM_1.1"], self.root, batch_size=101, downloader=self.downloader())
        with self.assertRaises(FetchError):
            OfficialRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com/download")


if __name__ == "__main__":
    unittest.main()
