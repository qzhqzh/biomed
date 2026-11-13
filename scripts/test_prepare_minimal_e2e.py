import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from apps.curation.decision import digest
from scripts.prepare_knowledge_intake import freeze
from scripts.prepare_minimal_e2e import (
    assemble_packets,
    deduplicate_exact_facts,
    freeze_snapshot_sources,
    screening_rows,
    selection_pmids,
)


class MinimalE2EPreparationTests(unittest.TestCase):
    def setUp(self):
        document = {"pmid": "123", "title": "Fixture title", "passages": [
            {"id": "p0", "version_id": "v1", "parser_version": "fixture-v1", "locator": "front/article-meta/abstract/p:1", "ordinal": 0, "text": "Abstract."},
            {"id": "p1", "version_id": "v1", "parser_version": "fixture-v1", "locator": "body/p:1", "ordinal": 1, "text": "Adjacent context."},
            {"id": "p2", "version_id": "v1", "parser_version": "fixture-v1", "locator": "body/p:2", "ordinal": 2, "text": "The result."},
            {"id": "p3", "version_id": "v1", "parser_version": "fixture-v1", "locator": "body/p:3", "ordinal": 3, "text": "Next context."},
        ]}
        self.rows = screening_rows(document, {"sha256": "a" * 64})
        self.dataset = {"records": self.rows}
        self.decisions = {row["id"]: {"id": row["id"], "input_sha256": row["input_sha256"],
                                     "status": "classified", "relevant": row["id"] in {"p2", "p3"}}
                          for row in self.rows}
        self.articles = {"123": {"pmid": "123", "title": "Fixture title", "version_id": "v1"}}

    def test_screening_protocol_preserves_neighbors_and_input_identity(self):
        middle = self.rows[2]
        self.assertEqual(middle["previous_text"], "Adjacent context.")
        self.assertEqual(middle["next_text"], "Next context.")
        self.assertEqual(middle["input_sha256"], digest({k: v for k, v in middle.items() if k != "input_sha256"}))

    def test_every_positive_is_processed_with_real_negative_context(self):
        packets, report = assemble_packets(self.dataset, self.decisions, self.articles)
        self.assertEqual(report["positive_packets"], 2)
        self.assertEqual(report["negative_passages"], 2)
        first = packets["records"][0]
        self.assertEqual(first["anchor_id"], "p2")
        self.assertEqual([p["id"] for p in first["passages"]], ["p0", "p1", "p2", "p3"])
        self.assertFalse(first["passages"][1]["upstream_relevant"])
        self.assertEqual(first["passages"][1]["role"], "support")
        self.assertEqual([p["id"] for p in first["passages"] if p["role"] == "anchor"], ["p2"])
        self.assertEqual(len({p["alias"] for p in first["passages"]}), 4)

    def test_unresolved_is_not_false_and_incomplete_screening_is_rejected(self):
        self.decisions["p3"].update(status="input_too_long", relevant=None)
        _, report = assemble_packets(self.dataset, self.decisions, self.articles)
        self.assertEqual(report["positive_packets"], 1)
        self.assertEqual(report["negative_passages"], 2)
        self.assertEqual(report["deferred_screening"][0]["id"], "p3")
        del self.decisions["p0"]
        with self.assertRaisesRegex(ValueError, "resume screening"):
            assemble_packets(self.dataset, self.decisions, self.articles)

    def test_wrong_input_hash_or_non_boolean_prediction_cannot_route(self):
        self.decisions["p2"]["input_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "different source"):
            assemble_packets(self.dataset, self.decisions, self.articles)
        self.decisions["p2"]["input_sha256"] = self.rows[2]["input_sha256"]
        self.decisions["p2"]["relevant"] = "true"
        with self.assertRaisesRegex(ValueError, "boolean"):
            assemble_packets(self.dataset, self.decisions, self.articles)

    def test_over_budget_retains_every_character_and_reports_instead_of_truncating(self):
        packets, report = assemble_packets(self.dataset, self.decisions, self.articles, max_context_characters=1)
        self.assertEqual(len(report["oversized_packets"]), 2)
        self.assertEqual(packets["records"][0]["passages"][2]["text"], self.rows[2]["text"])
        self.assertEqual(packets["records"][0]["context_budget_status"], "over_budget")

    def test_context_never_crosses_article_boundary(self):
        other = copy.deepcopy(self.rows[0])
        other.update(id="other", pmid="456", version_id="v2", text="Other article")
        other["input_sha256"] = digest({k: v for k, v in other.items() if k != "input_sha256"})
        self.dataset["records"].append(other)
        self.decisions["other"] = {"id": "other", "status": "classified", "relevant": True,
                                   "input_sha256": other["input_sha256"]}
        self.articles["456"] = {"pmid": "456", "title": "Second"}
        packets, _ = assemble_packets(self.dataset, self.decisions, self.articles)
        for packet in packets["records"]:
            self.assertEqual({p["pmid"] for p in packet["passages"]}, {packet["article_pmid"]})

    def test_nonadjacent_negative_eligibility_is_source_context_not_a_new_finding(self):
        item = {**self.rows[0], "id": "eligibility", "ordinal": 99, "locator": "body/p:99",
                "text": "Patients were eligible if they had Cancer X; a TEST V1A mutation; received Drug A."}
        item["input_sha256"] = digest({k: v for k, v in item.items() if k != "input_sha256"})
        self.rows.append(item)
        self.decisions[item["id"]] = {"id": item["id"], "input_sha256": item["input_sha256"], "status": "classified", "relevant": False}
        packets, report = assemble_packets(self.dataset, self.decisions, self.articles)
        self.assertEqual(report["positive_packets"], 2)
        passage = next(p for p in packets["records"][0]["passages"] if p["id"] == "eligibility")
        self.assertEqual(passage["text"], item["text"])
        self.assertEqual(passage["role"], "support")
        self.assertFalse(passage["upstream_relevant"])

    def test_positive_glossary_is_retained_as_context_but_not_a_center(self):
        original = copy.deepcopy(self.dataset)
        structure = {"p2": {"id": "p2", "locator": "body/p:2", "ancestor_tags": ["def", "def-item", "def-list"]}}
        packets, report = assemble_packets(self.dataset, self.decisions, self.articles, structure_by_passage=structure)
        self.assertEqual(self.dataset, original)
        self.assertEqual(report["screening_positive_passages"], 2)
        self.assertEqual(report["positive_packets"], 1)
        self.assertEqual(report["negative_passages"], 2)
        self.assertEqual(report["excluded_evidence_centers"][0]["original_screened_passage"], self.rows[2])
        self.assertEqual(packets["records"][0]["anchor_id"], "p3")
        glossary = next(p for p in packets["records"][0]["passages"] if p["id"] == "p2")
        self.assertEqual(glossary["role"], "support")
        self.assertIn("def-list", glossary["ancestor_tags"])

    def test_exact_dedup_retains_sources_and_does_not_merge_paraphrases_or_articles(self):
        base = {"group_id": "A", "version_id": "v1", "fact": {"conclusion": "Drug A benefits X",
                "entity_ids": ["e1"], "entity_roles": {"e1": ["intervention"]}, "evidence": ["P1"]},
                "entities": [{"ref": "e1", "kind": "drug", "label": "Drug A"}], "anchors": [{"quote": "first"}]}
        duplicate = copy.deepcopy(base)
        duplicate.update(group_id="B", anchors=[{"quote": "second"}])
        duplicate["entities"][0]["ref"] = "e2"
        duplicate["fact"].update(entity_ids=["e2"], entity_roles={"e2": ["intervention"]}, evidence=["P2"])
        paraphrase = copy.deepcopy(base)
        paraphrase.update(group_id="C")
        paraphrase["fact"]["conclusion"] = "X benefits from Drug A"
        another_article = copy.deepcopy(base)
        another_article.update(group_id="D", version_id="v2")
        bundle = {"records": [base, duplicate, paraphrase, another_article]}
        original = copy.deepcopy(bundle)
        reduced, report = deduplicate_exact_facts(bundle)
        self.assertEqual(bundle, original)
        self.assertEqual(report["retained_records"], 3)
        self.assertEqual(reduced["records"][0]["provenance"]["exact_duplicate_records"][0]["anchors"], [{"quote": "second"}])
        self.assertEqual(report["merged"][0]["group_id"], "B")

    def test_selection_file_formats_and_duplicate_validation(self):
        self.assertEqual(selection_pmids({"pmids": ["123", 456]}), ["123", "456"])
        self.assertEqual(selection_pmids({"records": [{"pmid": "123"}]}), ["123"])
        for bad in (["123", "123"], ["../123"], {"records": [{"material_id": "abc"}]}):
            with self.assertRaises(ValueError):
                selection_pmids(bad)

    def test_snapshot_freeze_preserves_missing_and_unusable_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = snapshot_fixture(root)
            plan = freeze_snapshot_sources(root / "batch", [fixture], ["123", "456", "789", "999"], root / "experiments")
            self.assertEqual(plan["requested_pmids"], ["123", "456", "789", "999"])
            self.assertEqual(plan["selected_pmids"], ["123"])
            self.assertEqual([s["status"] for s in plan["source_statuses"]],
                             ["ready_for_screening", "fulltext_missing", "source_unusable", "not_found_in_snapshots"])
            frozen = json.loads((root / "batch/screening/inputs.json").read_text())
            self.assertEqual([row["text"] for row in frozen["records"]], ["Source finding."])
            self.assertEqual(plan, freeze_snapshot_sources(root / "batch", [fixture], ["123", "456", "789", "999"], root / "experiments"))
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                freeze_snapshot_sources(root / "batch", [fixture], ["123"], root / "experiments")


def snapshot_fixture(root):
    """Small real JATS and frozen provenance; no database, network, or model."""
    library = root / "library"
    raw = b'<article><front><article-meta><article-id pub-id-type="pmid">123</article-id></article-meta></front><body><p>Source finding.</p></body></article>'
    metadata = b"metadata fixture"

    def blob(value):
        checksum = hashlib.sha256(value).hexdigest()
        path = library / "blobs" / checksum[:2] / checksum
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return checksum

    full_sha, meta_sha = blob(raw), blob(metadata)
    full = {"id": "v1", "sha256": full_sha, "integrity_status": "valid", "media_type": "application/jats+xml",
            "source_url": "https://example.test/paper", "rights": "fixture", "acquired_at": "2026-01-01"}
    meta = {"id": "m1", "sha256": meta_sha, "integrity_status": "valid", "media_type": "application/json"}
    passages = [{"id": "p1", "version_id": "v1", "parser_version": "jats-v1", "locator": "body/p:1",
                 "ordinal": 0, "text": "Source finding.", "kind": "fulltext"}]
    first = {"pmid": "123", "title": "Fixture", "material_id": "material1", "source_key": "source1",
             "versions": [full, meta], "fulltext_version_id": "v1", "metadata_version_id": "m1",
             "metadata": {}, "passages": passages, "passages_sha256": digest(passages), "input_scope": "parsed_fulltext"}
    missing = {"pmid": "456", "title": "Missing", "fulltext_version_id": None,
               "input_scope": "abstract_only", "source_issues": ["parsed_fulltext_missing"]}
    unusable = {**copy.deepcopy(first), "pmid": "789"}  # JATS PMID check fails.
    snapshot = {"readable_library": str(library), "documents": [first, missing, unusable]}
    path = root / "snapshot.json"
    freeze(path, {"snapshot": snapshot, "sha256": digest(snapshot)})
    freeze(root / "experiments/evidence-passages-20261023/definition.json", {"version": "fixture", "prompt": "test"})
    return path


if __name__ == "__main__":
    unittest.main()
