import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_fusion_pair_preview import preview


class SourceFusionPairPreviewTests(unittest.TestCase):
    def setUp(self):
        version = "12345678123456781234567812345678"
        source_sha = "a" * 64
        first = "Our case had an EML4-ALK fusion identified in tumor tissue."
        second = "Alectinib was given and the patient had a response lasting 9 months."
        passages = [{"id": "11111111111111111111111111111111", "locator": "body/p:1", "version_id": version,
                     "text": first},
                    {"id": "22222222222222222222222222222222", "locator": "body/p:2", "version_id": version,
                     "text": second}]
        drug_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        tumor_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        title = "Targeted treatment in melanoma: a case report"
        entities = [{"ref": "d", "kind": "disease", "label": "melanoma"},
                    {"ref": "t", "kind": "drug", "label": "Alectinib"},
                    {"ref": "m", "kind": "biomarker", "label": "EML4-ALK fusion"}]
        anchors = [{"purpose": "evidence", "passage_id": p["id"], "start": 0, "end": len(p["text"]),
                    "quote": p["text"], "attributes": {"alias": f"P{i}"}} for i, p in enumerate(passages, 1)]
        fact = {"scope": "original-study/case", "relation": "treatment_outcome",
                "population": "metastatic melanoma with EML4-ALK fusion", "treatment": "Alectinib",
                "drug_names": ["Alectinib"], "conditions": ["prior treatment"], "evidence": ["P1", "P2"],
                "entity_roles": {"d": ["disease"], "t": ["intervention"], "m": ["molecular"]},
                "results": [{"passage": "P2", "value": "9 months"}]}
        record = {"group_id": "synthetic", "version_id": version, "source_sha256": source_sha,
                  "article": {"pmid": "123", "title": title}, "fact": fact, "entities": entities, "anchors": anchors}
        document = {"fulltext_version_id": version, "versions": [{"id": version, "sha256": source_sha}],
                    "passages": passages, "passages_sha256": digest(passages),
                    "metadata": {"title": title, "publication_types": ["Journal Article"]}}
        self.sample = {"group_id": "synthetic", "record": record, "source_document": document,
                       "original_mapping": {"entity_ids": {"d": tumor_id, "t": drug_id}}}
        self.dictionary = {"schema_version": "tumor-api-dictionary-v1", "source": {"snapshot_id": "synthetic"},
                           "drugs": [{"id": drug_id}], "tumors": [{"id": tumor_id}], "rules": []}
        self.standard = {"symbols": {"EML4": {"symbol": "EML4"}, "ALK": {"symbol": "ALK"}}}

    def evaluate(self, sample=None):
        return preview(sample or self.sample, dictionary=self.dictionary, gene_standard=self.standard)

    def test_source_bound_pair_preserves_order_and_unknowns(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "inactive_structure_preview")
        target = result["target_payload"]
        self.assertFalse(target["is_active"])
        self.assertEqual(target["evidence_level"], "-")
        self.assertEqual(target["conclusion_inputs"][0]["potency"], "-")
        self.assertEqual(target["conclusion_inputs"][0]["trigger_inputs"][0],
                         {"trigger_type": "direct", "event_type": "fusion", "gene_symbol": "ALK",
                          "target_key": "EML4::ALK"})
        self.assertFalse(result["automatic_clinical_matching"])
        self.assertFalse(result["submit_ready"])

    def test_ambiguous_or_unbound_cases_do_not_create_previews(self):
        for kind in ("reverse", "different_drug", "review", "multiple_cases", "extra_gene", "no_result_source",
                     "source_changed", "unknown_id", "bad_population"):
            with self.subTest(kind=kind):
                sample = copy.deepcopy(self.sample)
                fact, record, doc = sample["record"]["fact"], sample["record"], sample["source_document"]
                if kind == "reverse":
                    fact["population"] = "metastatic melanoma with ALK-EML4 fusion"
                elif kind == "different_drug":
                    fact["treatment"] = "Alectinib plus another drug"
                elif kind == "review":
                    doc["metadata"]["publication_types"] = ["Review"]
                elif kind == "multiple_cases":
                    title = "Treatment in 2 patients: a case report"
                    record["article"]["title"] = doc["metadata"]["title"] = title
                elif kind == "extra_gene":
                    record["entities"].append({"ref": "other", "kind": "gene", "label": "TP53"})
                    fact["entity_roles"]["other"] = ["molecular"]
                elif kind == "no_result_source":
                    fact["results"][0]["passage"] = "P9"
                elif kind == "source_changed":
                    doc["passages"][0]["text"] += " changed"
                elif kind == "unknown_id":
                    sample["original_mapping"]["entity_ids"]["d"] = "cccccccc-cccc-cccc-cccc-cccccccccccc"
                else:
                    fact["population"] = "metastatic melanoma"
                self.assertEqual(self.evaluate(sample)["status"], "deferred")

    def test_negated_fusion_is_not_treated_as_positive(self):
        sample = copy.deepcopy(self.sample)
        text = "Our case had no EML4-ALK fusion identified in tumor tissue."
        p = sample["source_document"]["passages"][0]
        p["text"] = text
        sample["source_document"]["passages_sha256"] = digest(sample["source_document"]["passages"])
        anchor = sample["record"]["anchors"][0]
        anchor["quote"], anchor["end"] = text, len(text)
        self.assertEqual(self.evaluate(sample)["status"], "deferred")


if __name__ == "__main__":
    unittest.main()
