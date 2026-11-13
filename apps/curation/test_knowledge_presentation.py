import copy
import unittest

from apps.curation.knowledge_presentation import evidence_presentation


class KnowledgePresentationTests(unittest.TestCase):
    def setUp(self):
        self.fact = {"population": "patient with low-grade serous ovarian carcinoma",
                     "conditions": ["first-line therapy", "KRAS G12C mutation"],
                     "results": [{"measure": "response", "value": "partial response", "passage": "P1"}]}
        self.text = "Low-grade serous ovarian carcinoma received first-line therapy and achieved a partial response."
        self.source = {"passage_id": "p1", "start": 0, "end": len(self.text), "quote": self.text, "aliases": ["P1"],
                       "passage": {"id": "p1", "version_id": "v1", "locator": "body/p:1", "text": self.text}}

    def present(self, fact=None, sources=None):
        return evidence_presentation(fact or self.fact, sources if sources is not None else [self.source],
                                     version_id="v1", pmid="123")

    def test_unknown_grade_and_effect_are_not_inferred_from_response(self):
        data = self.present()
        self.assertEqual(data["evidence_level"]["label"], "未分级")
        self.assertEqual(data["drug_effect"]["label"], "未判定")
        self.assertTrue(data["informational_retrieval"])
        self.assertFalse(data["treatment_recommendation"])

    def test_known_source_bound_context_has_match_and_difference_choices(self):
        data = self.present()
        contexts = {item["field"]: item for item in data["reported_context"]["requirements"]}
        self.assertEqual(set(contexts), {"therapy_line", "histology"})
        choices = {choice["value"]: choice["status"] for choice in contexts["therapy_line"]["choices"]}
        self.assertEqual(choices["first-line therapy"], "consistent")
        self.assertEqual(choices["second-line therapy"], "different")
        source = contexts["histology"]["source"]
        self.assertEqual(self.text[source["start"]:source["end"]], source["quote"])
        self.assertEqual(source["version_id"], "v1")

    def test_wrong_version_wrong_span_or_unreviewed_field_does_not_produce_context(self):
        for edit in ("version", "quote", "fields", "passage_id", "end"):
            with self.subTest(edit=edit):
                source = copy.deepcopy(self.source)
                if edit == "version":
                    source["passage"]["version_id"] = "other"
                elif edit == "quote":
                    source["quote"] = "invented"
                elif edit == "fields":
                    source["fields"] = ["results"]
                elif edit == "passage_id":
                    source["passage_id"] = "other"
                else:
                    source["end"] += 5
                data = self.present(sources=[source])
                self.assertEqual(data["reported_context"]["requirements"], [])
                self.assertTrue(data["reported_context"]["unresolved"])

    def test_multiple_or_negated_treatment_lines_remain_unresolved(self):
        fact = copy.deepcopy(self.fact)
        fact["conditions"] = ["first-line therapy", "second-line therapy"]
        data = self.present(fact=fact)
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])
        source = copy.deepcopy(self.source)
        source["quote"] = source["passage"]["text"] = "The patient did not receive first-line therapy."
        source["end"] = len(source["quote"])
        data = self.present(sources=[source])
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])

    def test_treatment_line_uses_the_result_passage_not_general_background(self):
        fact = copy.deepcopy(self.fact)
        fact["results"][0]["passage"] = "P5"
        background = copy.deepcopy(self.source)
        background["aliases"] = ["P6"]
        result = copy.deepcopy(self.source)
        result["aliases"] = ["P5"]
        result["passage_id"] = result["passage"]["id"] = "result-passage"
        result["passage"]["locator"] = "body/p:9"
        data = self.present(fact=fact, sources=[background, result])
        requirement = next(item for item in data["reported_context"]["requirements"]
                           if item["field"] == "therapy_line")
        self.assertEqual(requirement["source"]["passage"], "result-passage")
        self.assertEqual(requirement["source"]["locator"], "body/p:9")
        data = self.present(fact=fact, sources=[background])
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])
        self.assertTrue(data["reported_context"]["unresolved"])
        ambiguous = copy.deepcopy(result)
        ambiguous["passage_id"] = ambiguous["passage"]["id"] = "another-passage"
        data = self.present(fact=fact, sources=[result, ambiguous])
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])
        fact["results"].append({"measure": "other result"})
        data = self.present(fact=fact, sources=[result])
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])
        fact["results"] = [{"measure": "result without source passage"}]
        data = self.present(fact=fact, sources=[background])
        self.assertNotIn("therapy_line", [item["field"] for item in data["reported_context"]["requirements"]])

    def test_unknown_conditions_remain_visible_and_not_interpreted(self):
        fact = {"population": "cancer patients", "conditions": ["recurrent disease", "prior therapy"]}
        result = self.present(fact=fact)
        self.assertEqual(result["reported_context"]["requirements"], [])
        self.assertTrue(result["reported_context"]["other_conditions_preserved"])
        self.assertFalse(result["reported_context"]["automatic_patient_eligibility"])

    def test_explicit_recorded_values_are_not_guessed_from_other_fields(self):
        fact = {"evidence_level": "B", "potency": "R", "direction": "positive"}
        data = self.present(fact=fact)
        self.assertEqual(data["evidence_level"]["label"], "已记录 B 级")
        self.assertEqual(data["drug_effect"]["label"], "已记录耐药")
        fact["potency"] = "positive response"
        self.assertEqual(self.present(fact=fact)["drug_effect"]["label"], "未判定")


if __name__ == "__main__":
    unittest.main()
