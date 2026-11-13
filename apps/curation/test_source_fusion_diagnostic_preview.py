import copy
import unittest

from apps.curation.decision import digest
from apps.curation.source_fusion_diagnostic_preview import preview
from apps.curation.test_source_fusion_pair_preview import SourceFusionPairPreviewTests


class SourceFusionDiagnosticPreviewTests(unittest.TestCase):
    def setUp(self):
        fixture = SourceFusionPairPreviewTests()
        fixture.setUp()
        self.sample = copy.deepcopy(fixture.sample)
        self.dictionary, self.standard = fixture.dictionary, fixture.standard
        self.dictionary['tumors'][0]['name_cn'] = '隆突性皮肤纤维肉瘤'
        self.references = {'terms': [{'status': 'reference', 'identifier': 'ONCOTREE:DFSP',
                                     'en': 'Dermatofibrosarcoma protuberans', 'zh': '隆突性皮肤纤维肉瘤',
                                     'aliases': ['DFSP'], 'sources': [{'url': 'https://example.org/reference'}]}]}
        record = self.sample['record']
        record['entities'][0]['label'] = 'Myxoid DFSP'
        record['assessment'] = {'execution': {'status': 'complete', 'issues': []}}
        record['fact'] = {'scope': 'original-study', 'relation': 'biomarker_association',
                          'population': 'Myxoid DFSP', 'treatment': None, 'drug_names': [],
                          'conditions': ['equivocal lesion'], 'evidence': ['P2'], 'entity_ids': ['d', 'm'],
                          'entity_roles': {'d': ['disease'], 'm': ['molecular']},
                          'results': [{'passage': 'P2', 'measure': 'diagnostic confirmation'}]}
        self.sample['source_document']['metadata']['publication_types'] = ['Case Reports']
        self.set_text(0, 'Dermatofibrosarcoma protuberans (DFSP) is a rare tumor.')
        self.set_text(1, 'In our case, the diagnosis of myxoid DFSP was confirmed by an EML4-ALK fusion.')

    def set_text(self, number, text):
        document = self.sample['source_document']
        document['passages'][number]['text'] = text
        document['passages_sha256'] = digest(document['passages'])
        anchor = self.sample['record']['anchors'][number]
        anchor['quote'], anchor['end'] = text, len(text)

    def evaluate(self):
        return preview(self.sample, dictionary=self.dictionary, gene_standard=self.standard,
                       name_references=self.references)

    def test_diagnostic_pair_and_source_defined_abbreviation(self):
        result = self.evaluate()
        self.assertEqual(result['status'], 'inactive_structure_preview')
        self.assertEqual(result['disease_identity']['qualifier'], 'Myxoid')
        self.assertEqual(result['disease_identity']['alias_definition']['quote'],
                         'Dermatofibrosarcoma protuberans (DFSP)')
        payload = result['target_payload']
        self.assertEqual(payload['evidence_category'], 'diagnostic')
        self.assertEqual(payload['evidence_level'], '-')
        self.assertFalse(payload['is_active'])
        self.assertEqual(payload['conclusion_inputs'][0]['drug_ids'], [])
        self.assertEqual(payload['conclusion_inputs'][0]['trigger_inputs'][0]['target_key'], 'EML4::ALK')
        self.assertFalse(result['automatic_clinical_matching'])

    def test_incomplete_ambiguous_or_other_case_does_not_create_preview(self):
        for kind in ('missing_alias', 'review', 'partial', 'other_case', 'negative_fusion', 'negative_disease',
                     'other_molecular_event', 'not_diagnostic', 'wrong_disease', 'ambiguous_target', 'source_changed',
                     'other_passage', 'qualifier_only_other_case', 'duplicate_entity'):
            with self.subTest(kind=kind):
                self.setUp()
                record, doc = self.sample['record'], self.sample['source_document']
                if kind == 'missing_alias':
                    self.set_text(0, 'Dermatofibrosarcoma protuberans is a rare tumor.')
                elif kind == 'review':
                    doc['metadata']['publication_types'].append('Review')
                elif kind == 'partial':
                    record['assessment']['execution']['status'] = 'partial'
                elif kind == 'other_case':
                    self.set_text(1, 'In our case, DFSP was diagnosed. Another patient had EML4-ALK fusion.')
                elif kind == 'negative_fusion':
                    self.set_text(1, 'In our case, the diagnosis of DFSP showed no EML4-ALK fusion.')
                elif kind == 'negative_disease':
                    self.set_text(1, 'In our case, no DFSP was diagnosed despite an EML4-ALK fusion.')
                elif kind == 'other_molecular_event':
                    record['entities'].append({'ref': 'g', 'kind': 'gene', 'label': 'OTHER'})
                    record['fact']['entity_roles']['g'] = ['molecular']
                elif kind == 'not_diagnostic':
                    record['fact']['results'] = [{'passage': 'P2', 'measure': 'overall survival'}]
                elif kind == 'wrong_disease':
                    record['entities'][0]['label'] = 'Myxoid DIFFERENT'
                elif kind == 'ambiguous_target':
                    self.dictionary['tumors'].append({'id': 'cccccccc-cccc-cccc-cccc-cccccccccccc',
                                                       'name_cn': '隆突性皮肤纤维肉瘤'})
                elif kind == 'other_passage':
                    record['fact']['results'][0]['passage'] = 'P1'
                elif kind == 'qualifier_only_other_case':
                    self.set_text(1, 'In our case, DFSP was diagnosed with EML4-ALK fusion. A prior case was myxoid.')
                elif kind == 'duplicate_entity':
                    record['entities'].append(copy.deepcopy(record['entities'][0]))
                else:
                    doc['passages'][0]['text'] += ' altered'
                self.assertEqual(self.evaluate()['status'], 'deferred')


if __name__ == '__main__':
    unittest.main()
