import unittest
from collections import Counter

from scripts.freeze_effective_knowledge_sample import select_sample


class EffectiveKnowledgeSampleTests(unittest.TestCase):
    def rows(self):
        records = []
        for status, prefix, count in (
            ("offline_ready", "ready", 3),
            ("mapping_needed", "mapping", 20),
            ("outside_target", "outside", 20),
            ("extraction_gap", "gap", 20),
        ):
            for n in range(count):
                records.append({"batch_id": f"batch-{prefix}-{n}", "pmid": f"{prefix}-{n}",
                                "group_id": f"{prefix}-{n}", "status": status})
        records.extend([
            {"batch_id": "known-mapping", "pmid": "known-1", "group_id": "E-42836110-0002", "status": "mapping_needed"},
            {"batch_id": "known-outside", "pmid": "known-2", "group_id": "E-42838582-0012", "status": "outside_target"},
        ])
        return records

    def test_is_reproducible_separated_and_keeps_known_cases_in_development(self):
        rows = self.rows()
        development, holdout = select_sample(rows)
        self.assertEqual((development, holdout), select_sample(list(reversed(rows))))
        self.assertEqual((len(development), len(holdout)), (20, 10))
        self.assertEqual(Counter(r["status"] for r in development + holdout),
                         {"offline_ready": 3, "mapping_needed": 9,
                          "outside_target": 9, "extraction_gap": 9})
        self.assertEqual(Counter(r["status"] for r in holdout),
                         {"mapping_needed": 3, "outside_target": 3, "extraction_gap": 4})
        self.assertEqual(len({r["pmid"] for r in development + holdout}), 30)
        self.assertTrue({"E-42836110-0002", "E-42838582-0012"}.issubset(
            {r["group_id"] for r in development}))

    def test_changed_positive_controls_fail_closed(self):
        rows = [row for row in self.rows() if row["group_id"] != "ready-0"]
        with self.assertRaisesRegex(ValueError, "three frozen"):
            select_sample(rows)


if __name__ == "__main__":
    unittest.main()
