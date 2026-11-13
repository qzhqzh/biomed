import copy
import unittest

from apps.curation.source_disease_projection import project_main_disease

TARGET = "11111111-1111-4111-8111-111111111111"


class SourceDiseaseProjectionTests(unittest.TestCase):
    def setUp(self):
        self.entity = {"ref": "disease", "kind": "disease", "label": "Low-grade serous ovarian carcinoma"}
        self.evidence = {"P1": {"passage_id": "p1", "start": 0, "end": 80,
                                "quote": "Low-grade serous ovarian carcinoma (LGSOC) is a subtype of ovarian cancer."}}
        self.nodes = [{"identifier": "ONCOTREE:LGSOC", "code": "LGSOC",
                       "en": "Low-Grade Serous Ovarian Cancer", "main_type": "Ovarian Cancer"}]
        self.dictionary = {"tumors": [{"id": TARGET, "name": "Ovarian Cancer", "name_cn": "卵巢癌"}]}
        self.source = {"sha256": "a" * 64}

    def project(self, prior_status="local_identity_target_missing"):
        return project_main_disease(self.entity, self.evidence, self.nodes, self.dictionary,
                                    prior_status=prior_status, ontology_source=self.source)

    def test_unique_source_abbreviation_and_standard_preserve_subtype(self):
        result = self.project()
        self.assertEqual(result["target_id"], TARGET)
        self.assertEqual(result["source_subtype"], self.entity["label"])
        self.assertEqual(result["standard_code"], "LGSOC")
        self.assertFalse(result["target_enforces_subtype"])

    def test_wrong_expansion_negation_or_conflicting_local_decision_does_not_project(self):
        self.nodes[0]["en"] = "Other Ovarian Cancer"
        self.assertIsNone(self.project())
        self.nodes[0]["en"] = "Low-Grade Serous Ovarian Cancer"
        self.evidence["P1"]["quote"] = "No low-grade serous ovarian carcinoma (LGSOC) was detected."
        self.assertIsNone(self.project())
        self.evidence["P1"]["quote"] = "Low-grade serous ovarian carcinoma (LGSOC) was detected."
        self.assertIsNone(self.project(prior_status="local_rule_unresolved"))

    def test_ambiguous_standard_or_target_is_not_guessed(self):
        self.nodes.append(copy.deepcopy(self.nodes[0]))
        self.assertIsNone(self.project())
        self.nodes.pop()
        self.dictionary["tumors"].append({"id": "22222222-2222-4222-8222-222222222222", "name": "Ovarian Cancer"})
        self.assertIsNone(self.project())


if __name__ == "__main__":
    unittest.main()
