import copy
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from apps.curation.classification import taxonomy_snapshot
from apps.curation.evaluation import input_hash, load_reviewed_samples, score_samples


class EvaluationTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        metadata = {
            "title": "Breast cancer",
            "abstract": "",
            "mesh": [],
            "publication_types": [],
        }
        self.row = {
            "pmid": "123",
            "version_id": "version-1",
            "source_sha256": "a" * 64,
            "input_sha256": input_hash(metadata),
            "group_id": "123",
            "stratum": "test",
            "split": "development",
            "metadata": metadata,
            "reference": {
                "scope": "solid_candidate",
                "publication_status": "unmarked",
                "relevance": "solid",
                "annotator": "test assistant",
                "reviewed_at": "2026-10-15T00:00:00Z",
                "rationale": "Title gives the disease.",
                "review_status": "assistant_reviewed_provisional",
                "human_reviewed": False,
                "evidence": [
                    {"field": "title", "start": 0, "end": 13, "text": "Breast cancer"}
                ],
            },
        }
        self.protocol = {"dataset_id": "test"}
        self.manifest = {
            "protocol": self.protocol,
            "records": [
                {
                    k: v
                    for k, v in self.row.items()
                    if k not in ("metadata", "reference")
                }
            ],
        }

    def load(self, rows=None):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        (self.root / "reviewed.json").write_text(
            json.dumps(
                {
                    "protocol": self.protocol,
                    "records": rows if rows is not None else [self.row],
                }
            )
        )
        return load_reviewed_samples(
            [self.root / "reviewed.json"], self.root / "manifest.json"
        )

    def test_valid_review_retains_provisional_identity(self):
        _, rows = self.load()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["reference"]["human_reviewed"])

    def test_changed_metadata_or_wrong_evidence_fails(self):
        for mutation in ("metadata", "offset", "review"):
            with self.subTest(mutation=mutation):
                row = copy.deepcopy(self.row)
                if mutation == "metadata":
                    row["metadata"]["title"] = "Lung cancer"
                elif mutation == "offset":
                    row["reference"]["evidence"][0]["end"] = 12
                else:
                    row["reference"]["review_status"] = "unreviewed"
                with self.assertRaises(ValueError):
                    self.load([row])

    def test_duplicates_and_related_groups_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复评测"):
            self.load([self.row, self.row])
        self.manifest["records"].append(
            {**self.manifest["records"][0], "pmid": "456", "split": "holdout"}
        )
        with self.assertRaisesRegex(ValueError, "关联组"):
            self.load()

    def test_missing_hard_cases_cannot_be_silently_skipped(self):
        self.manifest["records"].append(
            {**self.manifest["records"][0], "pmid": "456", "group_id": "456"}
        )
        with self.assertRaisesRegex(ValueError, "完整分组"):
            self.load()

    def test_agreement_and_coverage_use_separate_denominators(self):
        second = copy.deepcopy(self.row)
        second["pmid"] = "456"
        second["metadata"]["title"] = "Unspecified oncology research"
        # Known reference but insufficient model signal: counts as abstention and miss.
        scores = score_samples([self.row, second], taxonomy_snapshot())
        self.assertEqual(scores["scope_agreement"]["ratio"], 0.5)
        self.assertEqual(scores["decision_coverage"]["ratio"], 0.5)
        self.assertEqual(scores["decided_scope_agreement"]["ratio"], 1)
        self.assertEqual(
            scores["per_scope"]["benign_candidate"]["recall"]["ratio"], None
        )
