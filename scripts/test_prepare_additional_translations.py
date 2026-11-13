"""Source-identity and non-overwrite checks for the translation supplement."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "prepare_additional_translations",
    Path(__file__).with_name("prepare_additional_translations.py"),
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class TranslationSupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source/display-translations.json"
        self.rows = [
            {"identifier": "RXNORM:1", "en": "drug", "zh": None, "version": "v1"},
            {"identifier": "RXNORM:2", "en": "known", "zh": "旧名称", "version": "v1"},
            {
                "identifier": "RXNORM:3",
                "en": "drug hydrochloride",
                "zh": None,
                "version": "v1",
            },
        ]
        checksum = module.write_frozen(self.source, self.rows)
        module.write_frozen(
            self.source.parent / "display-manifest.json",
            {"state": "completed", "artifacts": {self.source.name: checksum}},
        )
        self.supplement = self.root / "supplement.json"
        self.payload = {
            "version": "translation-v1",
            "source_version": "v1",
            "entries": [
                {
                    "identifier": "RXNORM:1",
                    "en": "drug",
                    "zh": "药物",
                    "review_note": "限定词保留",
                },
                {"identifier": "RXNORM:2", "en": "known", "zh": "不得覆盖旧名称"},
            ],
        }

    def run_prepare(self):
        self.supplement.write_text(json.dumps(self.payload))
        return module.prepare("RXNORM", self.source, self.supplement, self.root / "out")

    def test_additive_and_resumable_preserves_notes_and_existing_names(self):
        result = self.run_prepare()
        self.assertEqual(result["counts"]["added_project_draft"], 1)
        self.assertEqual(result["counts"]["skipped_existing"], 1)
        self.assertEqual(result["counts"]["remaining_without_chinese"], 1)
        rows = module.read_json(self.root / "out/display-translations.json")
        self.assertEqual(rows[0]["review_note"], "限定词保留")
        self.assertEqual(rows[0]["status"], "project_draft")
        self.assertEqual(result, self.run_prepare())

    def test_rejects_english_identity_mismatch(self):
        self.payload["entries"][0]["en"] = "drug hydrochloride"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.run_prepare()

    def test_rejects_source_version_change(self):
        self.payload["source_version"] = "v2"
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            self.run_prepare()

    def test_rejects_duplicate_and_non_draft_status(self):
        self.payload["entries"].append(self.payload["entries"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.run_prepare()
        self.payload["entries"].pop()
        self.payload["entries"][0]["status"] = "confirmed"
        with self.assertRaisesRegex(ValueError, "project_draft"):
            self.run_prepare()

    def test_rejects_source_and_frozen_output_corruption(self):
        self.run_prepare()
        (self.root / "out/display-translations.json").write_text("[]")
        with self.assertRaisesRegex(ValueError, "output checksum"):
            self.run_prepare()
        self.source.write_text("[]")
        with self.assertRaisesRegex(ValueError, "Source display checksum"):
            self.run_prepare()

    def test_dictionary_does_not_strip_salt_or_adc_suffix(self):
        seed = self.root / "seed.tsv"
        seed.write_text("drug\t药物\n")
        headings = self.root / "headings.json"
        module.write_frozen(
            headings,
            {
                "pdf_sha256": "abc",
                "entries": [
                    {
                        "en": "DRUG",
                        "zh": "药物",
                        "pdf_page": 3,
                        "source_heading": "一、药物 DRUG",
                    },
                    {
                        "en": "antibody vedotin",
                        "zh": "偶联抗体",
                        "pdf_page": 5,
                        "source_heading": "二、偶联抗体 antibody vedotin",
                    },
                ],
            },
        )
        document = self.root / "document.json"
        module.write_frozen(
            document, {"sha256": "abc", "url": "https://example.org/source.pdf"}
        )
        result = module.build_drug_dictionary(
            self.source, seed, headings, document, self.root / "dictionary.json"
        )
        self.assertEqual([row["identifier"] for row in result["entries"]], ["RXNORM:1"])
        self.assertEqual(result["entries"][0]["verification"]["pdf_page"], 3)


if __name__ == "__main__":
    unittest.main()
