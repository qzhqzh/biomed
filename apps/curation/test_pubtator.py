"""Offline tests for rate limits, cache integrity, provenance and dual outputs."""

import copy
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from apps.curation.pubtator import (
    ENDPOINT,
    PARTIAL_TTL,
    RATE,
    Cache,
    PubTatorError,
    TransientError,
    atomic_json,
    build_reference,
    dictionary_indexes,
    download,
    project_passage,
    suggest,
)
from scripts.pubtator_reference import run_reference
from scripts.run_minimal_pipeline import pubtator_configuration


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


TEXT = "Patients with prostate cancer received Drug A targeting PSMA in this study."


def document(pmid="123", section="RESULTS"):
    return {"pmid": int(pmid), "passages": [{"offset": 17, "text": TEXT,
            "infons": {"section_type": section}, "annotations": [
                {"id": "a1", "text": "PSMA", "infons": {"type": "Gene", "identifier": "2346", "name": "FOLH1"},
                 "locations": [{"offset": 17 + TEXT.index("PSMA"), "length": 4}]}]}], "relations": []}


def raw(doc=None):
    return json.dumps({"PubTator3": [doc or document()]}).encode()


def standards():
    return {"genes": [{"gene_id": 2346, "symbol": "FOLH1", "synonyms": ["PSMA"]},
                      {"gene_id": 354, "symbol": "KLK3", "synonyms": ["PSA"]},
                      {"gene_id": 9520, "symbol": "NPEPPS", "synonyms": ["PSA"]}],
            "releases": {"r": {"namespace": "RXNORM"}},
            "concepts": [{"release_id": "r", "identifier": "RXNORM:1", "label": "Drug A"}]}


def packet():
    return {"id": "g1", "article": {"pmid": "123", "version_id": "v1", "source_sha256": "a" * 64},
            "passages": [{"id": "p1", "text": TEXT, "role": "anchor"}],
            "entities": [{"id": "g1.e1", "text": "Drug A", "type": "drug", "link": {"candidate_id": "RXNORM:1"}}],
            "entity_stage": {"model_status": "classified"}}


class PubTatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.calls = []

    def fetch(self, url):
        self.calls.append((url, self.clock.now()))
        ids = parse_qs(urlsplit(url).query)["pmids"][0].split(",")
        return json.dumps({"PubTator3": [document(p) for p in ids]}).encode()

    def client(self, fetcher=None):
        return Cache(self.root / "cache", fetcher=fetcher or self.fetch,
                     sleep=self.clock.sleep, clock=self.clock.now)

    def test_shared_cache_deduplicates_across_process_instances_and_views(self):
        first = self.client()
        result = first.get_many(["123", "123"], "full")
        first.get_many(["123"], "abstract")
        self.assertEqual(result["123"]["state"], "fulltext")
        second = self.client()
        self.assertTrue(second.get_many(["123"], "full")["123"]["cache_hit"])
        self.assertEqual(second.requests, 0)
        self.assertEqual(len(self.calls), 2)
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 2)
        self.assertEqual(len(list((first.root / "objects").glob("*.json"))), 1)

    def test_successful_subset_resume_only_requests_remaining_pmids(self):
        self.client().get_many(["123"], "full")
        result = self.client().get_many(["123", "456"], "full")
        self.assertTrue(result["123"]["cache_hit"])
        self.assertIn("pmids=456", self.calls[-1][0])

    def test_fulltext_fallback_is_explicit_and_expires(self):
        client = self.client(lambda url: raw(document(section="ABSTRACT")))
        self.assertEqual(client.get_many(["123"], "full")["123"]["state"], "abstract_only")
        self.clock.value += PARTIAL_TTL + 1
        client.get_many(["123"], "full")
        self.assertEqual(client.requests, 2)

    def test_transient_errors_wait_then_retry_and_clear_cooldown(self):
        attempts = []

        def flaky(url):
            attempts.append(self.clock.now())
            if len(attempts) < 3:
                raise TransientError("HTTP 503")
            return raw()

        client = self.client(flaky)
        self.assertEqual(client.get_many(["123"], "full")["123"]["state"], "fulltext")
        self.assertEqual(client.requests, 3)
        self.assertGreaterEqual(attempts[1] - attempts[0], 5)
        self.assertGreaterEqual(attempts[2] - attempts[1], 15)
        self.assertNotIn("retry_at", json.loads((client.root / "throttle.json").read_text()))

    def test_retry_after_does_not_block_main_pipeline_or_hammer_other_batches(self):
        def unavailable(url):
            raise TransientError("HTTP 429", retry_after=3600)

        client = self.client(unavailable)
        self.assertEqual(client.get_many(["123"], "full")["123"]["state"], "unavailable")
        other = self.client()
        self.assertEqual(other.get_many(["456"], "full")["456"]["reason"], "source_cooldown")
        self.assertEqual((client.requests, other.requests), (1, 0))
        self.assertLess(sum(self.clock.delays), 20)

    def test_missing_and_invalid_response_are_distinct(self):
        client = self.client(lambda url: b'{"PubTator3":[]}')
        self.assertEqual(client.get_many(["123"], "full")["123"]["state"], "missing")
        bad = self.client(lambda url: raw(document("999")))
        self.assertEqual(bad.get_many(["456"], "full")["456"]["state"], "unavailable")
        self.assertFalse((bad.root / "index/456-full.json").exists())

    def test_corrupt_cache_never_becomes_a_successful_annotation(self):
        entry = self.client().get_many(["123"], "full")["123"]
        Path(entry["response_file"]).write_bytes(b"corrupt")
        client = self.client()
        self.assertEqual(client.get_many(["123"], "full")["123"]["reason"], "cache_invalid")
        self.assertEqual(client.requests, 0)

    def test_concurrent_fetch_is_not_started_and_unsafe_pmids_are_rejected(self):
        client = self.client()
        client.root.mkdir()
        with (client.root / ".fetch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(client.get_many(["123"], "full")["123"]["reason"], "cache_busy")
        with self.assertRaises(ValueError):
            client.get_many(["../secrets"], "full")
        self.assertEqual(client.requests, 0)

    def test_http_reader_has_real_byte_pacing_and_fixed_endpoint(self):
        body = b"x" * (2 * RATE)
        response = io.BytesIO(body)
        response.headers = {}
        with patch("apps.curation.pubtator.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(download(ENDPOINT + "?pmids=123", sleep=self.clock.sleep,
                                      monotonic=self.clock.now), body)
        self.assertGreaterEqual(sum(self.clock.delays), 2)
        with self.assertRaises(PubTatorError):
            download("http://127.0.0.1/secrets")

    def test_projection_keeps_real_offsets_and_rejects_corrupt_remote_spans(self):
        source = packet()["passages"][0]
        source["text"] = source["text"].replace("PSMA", "ＰＳＭＡ")
        result = project_passage(source, document())
        annotation = result["annotations"][0]
        loc = annotation["location"]
        self.assertEqual(source["text"][loc["start"]:loc["end"]], "ＰＳＭＡ")
        broken = document()
        broken["passages"][0]["annotations"][0]["locations"][0]["offset"] += 1
        self.assertEqual(project_passage(source, broken)["annotations"], [])
        self.assertEqual(project_passage({**source, "text": "An unrelated article."}, document())["annotations"], [])

    def test_dictionary_gate_preserves_local_results_and_rejects_ambiguity(self):
        original = packet()
        saved = copy.deepcopy(original)
        projected = project_passage(original["passages"][0], document())["annotations"]
        for a in projected:
            a["source"] = {"pmid": "123"}
        projected += [{**projected[0], "text": "PSA", "infons": {"type": "Gene", "identifier": "354"}},
                      {**projected[0], "text": "prostate cancer", "infons": {"type": "Disease", "identifier": "MESH:wrong"}}]
        accepted, rejected = suggest(original, projected, dictionary_indexes(standards()))
        self.assertEqual([e["link"]["candidate_id"] for e in accepted], ["NCBIGene:2346"])
        self.assertEqual(len(rejected), 2)
        self.assertEqual(original, saved)
        self.assertTrue(all(e["relation_inferred"] is False for e in accepted))

    def test_failed_local_model_is_not_rescued_by_external_annotations(self):
        source = packet()
        source["entity_stage"]["model_status"] = "output_truncated"
        result = build_reference({"records": [source]}, self.client(), standards())
        self.assertEqual(result["records"][0]["candidates"], [])
        self.assertFalse(result["summary"]["local_models_skipped"])

    def test_shared_paragraph_retains_its_role_in_each_evidence_group(self):
        anchor, supporting = packet(), packet()
        supporting["id"] = "g2"
        supporting["passages"][0]["role"] = "support"
        result = build_reference({"records": [anchor, supporting]}, self.client(), standards())
        self.assertEqual([r["annotations"][0]["location"]["role"] for r in result["records"]],
                         ["anchor", "support"])

    def test_reference_only_uses_selected_packets_and_replays_without_network(self):
        batch = self.root / "batch"
        source = packet()
        selection = {"records": [{k: v for k, v in source.items() if k not in {"entities", "entity_stage"}}]}
        atomic_json(batch / "selected-batch/packets.json", selection)
        atomic_json(batch / "stages/entities/assembled-packets.json", {"records": [source]})
        dictionary = self.root / "standards.json"
        atomic_json(dictionary, standards())
        before = (batch / "stages/entities/assembled-packets.json").read_bytes()
        first = self.client()
        summary = run_reference(batch, cache=first, cache_root=first.root, standards=dictionary)
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(first.requests, 2)
        second = self.client(lambda url: self.fail("Replayed reference must not request data"))
        self.assertEqual(summary, run_reference(batch, cache=second, cache_root=second.root, standards=dictionary))
        self.assertEqual((batch / "stages/entities/assembled-packets.json").read_bytes(), before)
        self.assertTrue(all(parse_qs(urlsplit(url).query)["pmids"] == ["123"] for url, _ in self.calls))
        source["article"]["pmid"] = "456"
        atomic_json(batch / "stages/entities/assembled-packets.json", {"records": [source]})
        with self.assertRaisesRegex(ValueError, "source differs"):
            run_reference(batch, output=batch / "new-version", cache=second, standards=dictionary)

    def test_new_runs_enable_reference_but_legacy_resume_does_not_change(self):
        self.assertEqual(pubtator_configuration(self.root)["mode"], "dual_reference")
        atomic_json(self.root / "pipeline-manifest.json", {"version": "legacy"})
        self.assertIsNone(pubtator_configuration(self.root))
        chosen = pubtator_configuration(self.root, True, self.root / "cache")
        atomic_json(self.root / "pipeline-manifest.json", {"pubtator": chosen})
        self.assertEqual(pubtator_configuration(self.root), chosen)
        self.assertFalse(chosen["skip_local_models"])


if __name__ == "__main__":
    unittest.main()
