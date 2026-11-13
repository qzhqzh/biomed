import json
import tempfile
import unittest
from pathlib import Path

from scripts.freeze_fusion_independent_review import freeze, sha


class FusionReviewIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pmids = ['35664754', '41059188', '35941871']
        source = {'articles': [{'pmid': pmid, 'passages': [
            {'id': 'p' + pmid, 'locator': 'body/p:1', 'text': 'Exact source text for ' + pmid}]} for pmid in self.pmids]}
        self.write('source-only.json', source)
        self.write('astra-source-reference.json', {'test': 'synthetic reference'})
        self.write('new-candidates-comparison.json', {'records': [
            {'pmid': self.pmids[0], 'group_id': 'a', 'baseline_offline_ready': True,
             'treatment_preview': {'status': 'deferred'}, 'diagnostic_preview': {'status': 'deferred'}},
            {'pmid': self.pmids[1], 'group_id': 'b', 'baseline_offline_ready': False,
             'treatment_preview': {'status': 'inactive_structure_preview'}, 'diagnostic_preview': {'status': 'deferred'}},
        ]})
        def citation(pmid):
            return {'passage_id': 'p' + pmid, 'locator': 'body/p:1', 'quote': 'source text for ' + pmid}
        self.review = {'model': 'gpt-6-astra',
                       'input_sha256': {name: sha(self.root/name) for name in (
                           'source-only.json', 'astra-source-reference.json', 'new-candidates-comparison.json')},
                       'records': [{'pmid': pmid, 'group_id': group, 'independent_fact_id': group + '-fact',
                                    'core_source_fact_judgment': 'supported', 'target_structure_judgment': 'partial',
                                    'evidence': [citation(pmid)]}
                                   for pmid, group in zip(self.pmids, ['a', 'b'])],
                       'omissions': [{'pmid': self.pmids[2], 'independent_fact_id': 'missing',
                                      'evidence': [citation(self.pmids[2])]}]}

    def write(self, name, value):
        (self.root/name).write_text(json.dumps(value), encoding='utf-8')

    def test_valid_review_is_frozen_and_can_be_reused(self):
        self.write('review.json', self.review)
        result = freeze(self.root, self.root/'review.json')
        self.assertEqual((result['structure_candidate_groups'], result['distinct_reviewed_facts'],
                          result['distinct_omitted_facts'], result['source_quotes_verified']), (2, 2, 1, 3))
        self.assertEqual(freeze(self.root, self.root/'review.json'), result)

    def test_wrong_quote_and_unreviewed_candidate_are_rejected(self):
        self.review['records'][0]['evidence'][0]['quote'] = 'Not in this passage'
        self.write('review.json', self.review)
        with self.assertRaisesRegex(ValueError, 'quotation'):
            freeze(self.root, self.root/'review.json')
        self.review['records'][0]['evidence'][0]['quote'] = 'Exact source'
        self.review['records'].pop()
        self.write('review.json', self.review)
        with self.assertRaisesRegex(ValueError, 'cover every candidate'):
            freeze(self.root, self.root/'review.json')


if __name__ == '__main__':
    unittest.main()
