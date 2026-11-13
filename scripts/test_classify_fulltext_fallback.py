import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.curation.decision import digest
from scripts.classify_fulltext_fallback import metadata_signals, prepare, run
from scripts.prepare_knowledge_intake import freeze


def fixture(root):
    library = root / "library"
    safety = {"title": "Immune checkpoint inhibitor toxicity in cancer",
              "abstract": "A case of immune-related adverse events after treatment.",
              "mesh": [], "publication_types": ["Case Reports"]}
    other = {"title": "Inflammatory arthritis", "abstract": "A case report.", "mesh": [], "publication_types": []}

    def blob(value):
        checksum = hashlib.sha256(value).hexdigest()
        path = library / "blobs" / checksum[:2] / checksum
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return checksum

    def document(pmid, metadata, *, fulltext=True):
        meta_sha = blob(json.dumps(metadata).encode())
        meta_id, full_id = f"meta-{pmid}", f"full-{pmid}"
        model_input = {"pmid": pmid, "metadata": copy.deepcopy(metadata), "version_id": meta_id,
                       "source_sha256": meta_sha, "input_sha256": digest(metadata)}
        result = {"pmid": pmid, "title": metadata["title"], "material_id": f"material-{pmid}",
                  "source_key": f"source-{pmid}", "metadata": copy.deepcopy(metadata), "model_input": model_input,
                  "metadata_version_id": meta_id, "fulltext_version_id": None, "input_scope": "abstract_only",
                  "versions": [{"id": meta_id, "sha256": meta_sha, "integrity_status": "valid", "media_type": "application/json"}],
                  "passages": [], "source_issues": ["parsed_fulltext_missing"]}
        if not fulltext:
            return result
        paragraphs = ["A first complete paragraph.", "Melanoma was treated; the second paragraph is complete."]
        raw = (f'<article><front><article-meta><article-id pub-id-type="pmid">{pmid}</article-id>'
               f'</article-meta></front><body><p>{paragraphs[0]}</p><p>{paragraphs[1]}</p></body></article>').encode()
        full_sha = blob(raw)
        passages = [{"id": f"{pmid}-p{index}", "version_id": full_id, "parser_version": "jats-v1", "ordinal": index - 1,
                     "locator": f"body/p:{index}", "text": text, "kind": "fulltext"}
                    for index, text in enumerate(paragraphs, 1)]
        result.update(fulltext_version_id=full_id, input_scope="parsed_fulltext", passages=passages,
                      passages_sha256=digest(passages), source_issues=[])
        result["versions"].append({"id": full_id, "sha256": full_sha, "integrity_status": "valid",
                                   "media_type": "application/jats+xml", "rights": "fixture",
                                   "source_url": "https://example.test", "acquired_at": "2026-01-01"})
        return result

    documents = [document("123", safety), document("222", safety), document("456", safety, fulltext=False),
                 document("789", safety), document("321", other), document("654", safety)]
    snapshot = {"readable_library": str(library), "documents": documents}
    snapshot_path = root / "snapshot.json"
    freeze(snapshot_path, {"sha256": digest(snapshot), "snapshot": snapshot})
    selection = {"records": [{"pmid": "123", "status": "classified", "category": "non_neoplastic"},
                             {"pmid": "222", "status": "abstained", "category": None},
                             {"pmid": "456", "status": "classified", "category": "non_neoplastic"},
                             {"pmid": "789", "status": "classified", "category": "hematologic"},
                             {"pmid": "321", "status": "classified", "category": "non_neoplastic"},
                             {"pmid": "654", "status": "classified", "category": "solid"},
                             {"pmid": "987", "status": "abstained", "category": None}]}
    selection_path = root / "selection.json"
    freeze(selection_path, selection)
    definition = {"categories": ["solid", "hematologic", "non_neoplastic"],
                  "prompt": "Use only the supplied title, abstract, MeSH and publication types. Categories unchanged."}
    definition_path = root / "definition.json"
    freeze(definition_path, definition)
    return snapshot_path, selection_path, definition_path, documents


class FulltextFallbackTests(unittest.TestCase):
    def test_metadata_gate_is_narrow_and_never_assigns_a_category(self):
        matched = metadata_signals({"title": "Immune checkpoint inhibitor toxicity", "abstract": "",
                                    "mesh": [], "publication_types": []})
        self.assertTrue(matched["candidate"])
        self.assertFalse(metadata_signals({"title": "Cancer immunotherapy", "abstract": "Efficacy results",
                                           "mesh": [], "publication_types": []})["candidate"])
        self.assertFalse(metadata_signals({"title": "Arthritis treatment adverse effects", "abstract": "",
                                           "mesh": [], "publication_types": []})["candidate"])
        self.assertNotIn("category", matched)

    def test_prepare_preserves_full_paragraphs_and_original_classification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot, selection, definition, documents = fixture(root)
            before = selection.read_bytes()
            output = root / "fallback"
            plan = prepare([snapshot], selection, output, pmids=["123", "456"], definition_path=definition)
            statuses = {row["pmid"]: row["fallback_status"] for row in plan["records"]}
            self.assertEqual(statuses, {"123": "prepared", "222": "candidate_not_selected", "456": "fulltext_missing",
                                        "789": "already_hematologic", "321": "not_a_safety_candidate",
                                        "654": "already_solid", "987": "not_found_in_snapshots"})
            self.assertEqual(selection.read_bytes(), before)
            row = json.loads((output / "inputs.json").read_text())["records"][0]
            prompt = json.loads(row["prompt"])
            self.assertEqual(prompt["metadata"], documents[0]["metadata"])
            self.assertEqual(prompt["parsed_fulltext_paragraphs"],
                             [{k: p[k] for k in ("id", "locator", "ordinal", "text")} for p in documents[0]["passages"]])
            self.assertEqual(row["fulltext_source_sha256"], documents[0]["versions"][1]["sha256"])
            self.assertEqual(plan["prepared_inputs"], 1)
            self.assertFalse(plan["input_truncation"])
            self.assertEqual(plan, prepare([snapshot], selection, output, pmids=["123", "456"], definition_path=definition))

    def test_token_overflow_stays_unresolved_and_other_result_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot, selection, definition_path, _ = fixture(root)
            output = root / "fallback"
            prepare([snapshot], selection, output, definition_path=definition_path)
            model = root / "fake-model"
            freeze(model / "download-manifest.json", {"repo": "Qwen/Qwen3.5-4B", "revision": "fixed"})

            def fake_execute(command, logfile):
                self.assertIn("--resume", command)
                self.assertEqual(command[command.index("--max-input-tokens") + 1], "6144")
                source = json.loads((output / "inputs.json").read_text())
                definition = json.loads((output / "definition.json").read_text())
                config = {"definition": definition, "max_input_tokens": 6144, "input_truncation": False,
                          "model_id": "Qwen/Qwen3.5-4B", "revision": "fixed"}
                extraction = output / "extract"
                freeze(extraction / "inputs.json", source)
                freeze(extraction / "manifest.json", {"input_sha256": digest(source), "definition_sha256": digest(definition),
                                                       "config": config, "config_sha256": digest(config)})
                freeze(extraction / "summary.json", {"state": "completed"})
                for row in source["records"]:
                    too_long = row["pmid"] == "222"
                    result = {"id": row["id"], "input_sha256": row["input_sha256"], "config_sha256": digest(config),
                              "status": "input_too_long" if too_long else "classified",
                              "payload": None if too_long else {"category": "solid"},
                              "raw_output": "" if too_long else '{"category":"solid"}',
                              "input_tokens": 7000 if too_long else 100, "output_tokens": 0 if too_long else 5, "seconds": 0.1}
                    freeze(extraction / f"result-{row['id']}.json", result)

            with patch("scripts.classify_fulltext_fallback.execute_command", side_effect=fake_execute):
                result = run(output, model=model)
                self.assertEqual(result, run(output, model=model))
            rows = {row["pmid"]: row for row in result["records"]}
            self.assertEqual(result["state"], "completed_with_unresolved")
            self.assertEqual(rows["123"]["fallback_category"], "solid")
            self.assertEqual(rows["222"]["fallback_status"], "input_too_long")
            self.assertIsNone(rows["222"]["fallback_category"])
            self.assertEqual(result["continue_pmids"], ["123"])
            self.assertEqual(json.loads((output / "continue-pmids.json").read_text()), {"pmids": ["123"]})
            self.assertEqual(rows["123"]["original_category"], "non_neoplastic")

    def test_no_candidates_skips_model_and_preserves_empty_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot, selection, definition, _ = fixture(root)
            output = root / "fallback"
            plan = prepare([snapshot], selection, output, pmids=["321"], definition_path=definition)
            self.assertEqual(plan["prepared_inputs"], 0)
            with patch("scripts.classify_fulltext_fallback.execute_command", side_effect=AssertionError("unexpected model call")):
                result = run(output, model=root / "missing-model")
            self.assertEqual(result["continue_pmids"], [])
            self.assertEqual(result["fallback_statuses"]["candidate_not_selected"], 3)

    def test_unusable_fulltext_does_not_discard_other_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot, selection, definition, documents = fixture(root)
            checksum = documents[0]["versions"][1]["sha256"]
            (root / "library/blobs" / checksum[:2] / checksum).write_bytes(b"damaged")
            plan = prepare([snapshot], selection, root / "fallback", definition_path=definition)
            statuses = {row["pmid"]: row["fallback_status"] for row in plan["records"]}
            self.assertEqual(statuses["123"], "source_unusable")
            self.assertEqual(statuses["222"], "prepared")

    def test_original_classifier_revision_must_match_before_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot, selection, definition, _ = fixture(root)
            config = {"model_id": "Qwen/Qwen3.5-4B", "revision": "original"}
            freeze(root / "extract/manifest.json", {"config": config, "config_sha256": digest(config)})
            output = root / "fallback"
            prepare([snapshot], selection, output, pmids=["123"], definition_path=definition)
            model = root / "fake-model"
            freeze(model / "download-manifest.json", {"repo": "Qwen/Qwen3.5-4B", "revision": "different"})
            with (patch("scripts.classify_fulltext_fallback.execute_command", side_effect=AssertionError("unexpected model call")),
                  self.assertRaisesRegex(ValueError, "original classifier revision")):
                run(output, model=model)


if __name__ == "__main__":
    unittest.main()
