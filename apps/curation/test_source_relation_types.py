import unittest
from copy import deepcopy

from apps.curation.source_relation_types import refine_relation


class SourceRelationTypeTests(unittest.TestCase):
    def fixture(self):
        packet = {"passages": [{"id": "p1", "alias": "P1", "role": "anchor", "locator": "body/p:1",
            "version_id": "v1", "source_sha256": "a" * 64,
            "text": "Molecular features improved model discrimination. DCA showed greater net benefit for the integrated model."}]}
        fact = {"relation": "treatment_outcome", "treatment": "neoadjuvant chemotherapy", "population": "breast cancer receiving NAC",
                "conditions": ["exploratory; external validation is required"], "evidence": ["P1"],
                "results": [{"measure": "net benefit in decision curve analysis", "group": "integrated model vs cN-only model",
                             "value": "greater net benefit", "passage": "P1"}]}
        return packet, fact

    def test_model_performance_changes_only_relation_and_preserves_all_conditions(self):
        packet, fact = self.fixture()
        original = deepcopy((packet, fact))
        result = refine_relation(packet, fact)
        self.assertEqual(result["fact"], {**fact, "relation": "biomarker_association"})
        self.assertEqual((packet, fact), original)
        self.assertEqual(result["changes"][0]["fields"], {"relation": {"before": "treatment_outcome", "after": "biomarker_association"}})
        quote = result["changes"][0]["evidence"][0]
        self.assertEqual(packet["passages"][0]["text"][quote["start"]:quote["end"]], quote["quote"])

    def test_auc_alone_pharmacokinetics_mixed_outcomes_or_missing_source_abstain(self):
        packet, fact = self.fixture()
        cases = []
        p, f = deepcopy(packet), deepcopy(fact)
        p["passages"][0]["text"] = "The pharmacokinetic model showed increased AUC after NAC."
        f["results"][0]["measure"] = "AUC"
        cases.append((p, f))
        p, f = deepcopy(packet), deepcopy(fact)
        p["passages"][0]["text"] += " The model also evaluated drug exposure."
        cases.append((p, f))
        p, f = deepcopy(packet), deepcopy(fact)
        f["results"].append({"measure": "overall survival", "group": "NAC", "value": "improved", "passage": "P1"})
        cases.append((p, f))
        p, f = deepcopy(packet), deepcopy(fact)
        f["results"][0]["passage"] = "P2"
        cases.append((p, f))
        p, f = deepcopy(packet), deepcopy(fact)
        f["results"][0]["group"] = "patients receiving NAC"
        cases.append((p, f))
        for p, f in cases:
            with self.subTest(fact=f):
                self.assertEqual(refine_relation(p, f), {"fact": f, "changes": []})


if __name__ == "__main__":
    unittest.main()
