import json
import tempfile
from pathlib import Path
from unittest import TestCase

from apps.curation.cancer_resources import (
    allowed,
    display_overlay,
    flatten_tree,
    parse_wikidata,
    stable_release,
    translations,
    wikidata_query,
)


class CancerResourceTests(TestCase):
    def node(self, name="Lung Adenocarcinoma"):
        return {"identifier": "ONCOTREE:LUAD", "en": name, "nci_references": ["C3512"]}

    def wikidata(self, qid="Q843799", name="Lung adenocarcinoma"):
        return {
            "qid": qid,
            "nci": "C3512",
            "english": [name],
            "chinese": {"zh": "肺腺癌"},
        }

    def test_shared_nci_reference_alone_is_not_a_translation_match(self):
        rows = translations([self.node()], [self.wikidata(name="Adenocarcinoma")], "v1")
        self.assertIsNone(rows[0]["zh"])
        self.assertEqual(rows[0]["status"], "missing")

    def test_specific_subtype_is_not_replaced_by_broad_parent(self):
        rows = translations(
            [self.node("Lung Adenocarcinoma, Micropapillary")], [self.wikidata()], "v1"
        )
        self.assertIsNone(rows[0]["zh"])

    def test_exact_english_alias_and_nci_match_gives_reference_not_official(self):
        row = translations([self.node()], [self.wikidata()], "v1")[0]
        self.assertEqual(row["zh"], "肺腺癌")
        self.assertEqual(row["status"], "reference")
        self.assertEqual(row["source_url"], "https://www.wikidata.org/wiki/Q843799")
        self.assertEqual(row["rights"], "CC0 1.0")

    def test_ambiguous_items_are_not_resolved_by_same_chinese_string(self):
        row = translations(
            [self.node()], [self.wikidata(), self.wikidata(qid="Q1")], "v1"
        )[0]
        self.assertEqual(row["status"], "ambiguous")
        self.assertIsNone(row["zh"])
        self.assertEqual(len(row["candidates"]), 2)

    def test_simplified_label_is_preferred_but_language_is_preserved(self):
        item = self.wikidata()
        item["chinese"] = {"zh": "肺腺癌", "zh-hans": "肺腺癌", "zh-hant": "肺腺癌"}
        row = translations([self.node()], [item], "v1")[0]
        self.assertEqual(row["language"], "zh-hans")

    def test_missing_chinese_does_not_become_english_fallback(self):
        item = self.wikidata()
        item["chinese"] = {}
        row = translations([self.node()], [item], "v1")[0]
        self.assertEqual(row["status"], "missing")
        self.assertIsNone(row["zh"])

    def test_query_rejects_injection_and_unbounded_request(self):
        for codes in (['C3512" } UNION {'], [f"C{i}" for i in range(76)], []):
            with self.assertRaises(ValueError):
                wikidata_query(codes)

    def test_wikidata_unrequested_nci_and_invalid_label_are_rejected(self):
        row = {
            "nci": {"value": "C3512"},
            "item": {"value": "http://www.wikidata.org/entity/Q843799"},
            "english": {"value": "Lung adenocarcinoma", "xml:lang": "en"},
        }
        with self.assertRaises(ValueError):
            parse_wikidata({"results": {"bindings": [row]}}, {"C1"})
        row["english"]["xml:lang"] = "zh"
        with self.assertRaises(ValueError):
            parse_wikidata({"results": {"bindings": [row]}}, {"C3512"})

    def test_current_stable_must_have_fixed_version(self):
        versions = [
            {"api_identifier": "oncotree_latest_stable", "release_date": "2025-10-03"}
        ]
        with self.assertRaises(ValueError):
            stable_release(versions)
        versions.append(
            {"api_identifier": "oncotree_2025_10_03", "release_date": "2025-10-03"}
        )
        self.assertEqual(
            stable_release(versions)["api_identifier"], "oncotree_2025_10_03"
        )

    def test_invalid_hierarchy_does_not_become_a_snapshot(self):
        tree = {
            "TISSUE": {
                "code": "TISSUE",
                "name": "Tissue",
                "level": 0,
                "parent": None,
                "externalReferences": {},
                "children": {
                    "LUNG": {
                        "code": "LUNG",
                        "name": "Lung",
                        "parent": "OTHER",
                        "level": 1,
                        "externalReferences": {},
                        "children": {},
                    }
                },
            }
        }
        with self.assertRaises(ValueError):
            flatten_tree(tree)

    def test_fetch_allowlist_is_exact_and_https_only(self):
        allowed("https://query.wikidata.org/sparql?format=json")
        for url in (
            "http://query.wikidata.org/sparql",
            "https://query.wikidata.org.evil.test/",
            "https://x@query.wikidata.org/sparql",
            "https://query.wikidata.org:444/sparql",
        ):
            with self.assertRaises(ValueError):
                allowed(url)

    def test_known_inaccurate_reference_is_kept_raw_but_displayed_as_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            version = "oncotree_2025_10_03"
            source = {
                "identifier": "ONCOTREE:HCC",
                "en": "Hepatocellular Carcinoma",
                "zh": "肝臟上皮細胞腫瘤",
                "source": "Wikidata",
                "status": "reference",
                "official_source_url": f"https://oncotree.mskcc.org/api/tumorTypes/tree?version={version}",
            }
            raw = json.dumps([source], ensure_ascii=False)
            (base / "translations.json").write_text(raw)
            (base / "manifest.json").write_text(
                json.dumps({"oncotree_version": version})
            )
            result = display_overlay(base)
            row = json.loads((base / "display-translations.json").read_text())[0]
            self.assertEqual(row["zh"], "肝细胞癌")
            self.assertEqual(row["status"], "project_draft")
            self.assertEqual(row["provider"], "biomed project translation")
            self.assertEqual((base / "translations.json").read_text(), raw)
            self.assertEqual(result["counts"]["excluded_reference_labels"], 1)
            self.assertEqual(display_overlay(base), result)

    def test_frozen_display_corruption_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "display-translations.json").write_text("changed")
            (base / "display-manifest.json").write_text(
                json.dumps({"artifacts": {"display-translations.json": "wrong"}})
            )
            with self.assertRaises(ValueError):
                display_overlay(base)
