import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_context_retrieval import propose_support
from apps.curation.test_source_linked_facts import VERSION_ID, SourceLinkedFactTests


class SourceContextRetrievalTests(unittest.TestCase):
    def setUp(self):
        fixture = SourceLinkedFactTests()
        fixture.setUp()
        self.sample = fixture.sample
        self.sample["record"]["fact"] = {"results": [{"value": "24.9 months"}]}
        passages = self.sample["source_document"]["passages"]
        passages.extend([
            {"id": "77777777-7777-4777-8777-777777777777", "version_id": VERSION_ID, "locator": "body/p:3",
             "text": "For TEST V1A, first-line Drug A had a median PFS of 24.9 months."},
            {"id": "88888888-8888-4888-8888-888888888888", "version_id": VERSION_ID, "locator": "body/p:4",
             "text": "Patients with TEST V1A receiving Drug A had 12 months of follow-up."},
        ])
        self.sample["source_document"]["passages_sha256"] = digest(passages)

    def test_retrieves_only_new_exact_context_without_changing_sample(self):
        before = copy.deepcopy(self.sample)
        result = propose_support(self.sample, variant="V1A", drug="Drug A")
        self.assertEqual([row["locator"] for row in result["candidates"]], ["body/p:3"])
        self.assertFalse(result["semantic_support_verified"])
        self.assertFalse(result["target_approved"])
        self.assertEqual(self.sample, before)

    def test_missing_values_or_wrong_variant_do_not_invent_support(self):
        self.assertEqual(propose_support(self.sample, variant="V2A", drug="Drug A")["candidates"], [])
        self.sample["record"]["fact"]["results"] = []
        self.assertEqual(propose_support(self.sample, variant="V1A", drug="Drug A")["candidates"], [])

    def test_tampered_source_is_rejected(self):
        self.sample["source_document"]["passages"][2]["text"] = "altered"
        with self.assertRaisesRegex(ValueError, "passages differ"):
            propose_support(self.sample, variant="V1A", drug="Drug A")
