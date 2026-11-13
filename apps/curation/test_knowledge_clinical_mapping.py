import copy
import unittest

from apps.curation import test_knowledge_target_mapping as fixtures
from apps.curation.knowledge_clinical_mapping import adapt
from apps.curation.knowledge_export import export_bundle
from apps.curation.knowledge_target_mapping import prepare_mappings


class ClinicalMappingTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.KnowledgeTargetMappingTests()
        fixture.setUp()
        self.bundle = copy.deepcopy(fixture.bundle)
        self.record = self.bundle['records'][0]
        self.dictionary = copy.deepcopy(fixture.dictionary)
        self.standard = copy.deepcopy(fixture.gene_standard)
        self.standard['aliases'] = {'OLDTEST': 'TEST'}
        self.record['entities'] = [e for e in self.record['entities'] if e['ref'] != 'drug']
        self.record['entities'][-1]['label'] = 'OLDTEST V1A'
        self.record['fact'].update(relation='biomarker_association', population='Cancer X', treatment=None,
                                   drug_names=[], conditions=['OLDTEST V1A'],
                                   results=[{'measure': 'molecular diagnosis', 'value': 'Pathology confirmed Cancer X with OLDTEST V1A', 'passage': 'P1'}],
                                   entity_roles={'cancer': ['disease'], 'variant': ['molecular']})
        self.quote('Pathology confirmed Cancer X with OLDTEST V1A mutation in this patient.')

    def quote(self, text):
        self.record['anchors'][0].update(quote=text, end=len(text))

    def result(self):
        return adapt(self.bundle, self.dictionary, None, self.standard)[1]['records'][0]

    def test_diagnostic_alias_is_opt_in_and_preserves_source(self):
        before = copy.deepcopy(self.bundle)
        mappings, _ = prepare_mappings(self.bundle, self.dictionary, gene_standard=self.standard)
        self.assertFalse(export_bundle(self.bundle, dictionary=self.dictionary, mappings=mappings)['records'][0]['offline_ready'])
        row = self.result()
        self.assertTrue(row['offline_ready'], row['mapping_issues'])
        self.assertEqual(row['target_payload']['evidence_category'], 'diagnostic')
        self.assertEqual(row['target_payload']['conclusion_inputs'][0]['drug_ids'], [])
        self.assertEqual(self.bundle, before)

    def test_expression_background_and_treatment_are_not_diagnostic(self):
        for field, value in [('relation', 'mechanism'), ('treatment', 'Drug A'), ('drug_names', ['Drug A']),
                             ('results', [{'measure': 'expression', 'value': 'high protein expression'}])]:
            with self.subTest(field=field):
                old = self.record['fact'][field]
                self.record['fact'][field] = value
                self.assertFalse(self.result()['offline_ready'])
                self.record['fact'][field] = old

    def test_negated_event_is_not_a_positive_trigger(self):
        self.quote('Pathology confirmed Cancer X, without OLDTEST V1A mutation.')
        self.assertFalse(self.result()['offline_ready'])
        self.quote('Pathology confirmed Cancer X. OLDTEST V1A was not detected.')
        self.assertFalse(self.result()['offline_ready'])
        self.quote('Pathology confirmed Cancer X. OLDTEST V1A mutation was not detected.')
        self.assertFalse(self.result()['offline_ready'])

    def test_gene_level_rearrangement_does_not_invent_partner(self):
        self.record['entities'][-1]['label'] = 'TEST gene rearrangement'
        self.record['fact'].update(conditions=['TEST gene rearrangement'],
                                  results=[{'measure': 'molecular diagnosis', 'value': 'Confirmed Cancer X with TEST gene rearrangement', 'passage': 'P1'}])
        self.quote('Pathology confirmed Cancer X with a TEST gene rearrangement by FISH.')
        row = self.result()
        self.assertTrue(row['offline_ready'], row['mapping_issues'])
        trigger = row['target_payload']['conclusion_inputs'][0]['trigger_inputs'][0]
        self.assertEqual(trigger, {'trigger_type': 'direct', 'event_type': 'fusion', 'gene_symbol': 'TEST', 'target_key': 'Fusion'})
        self.quote('Pathology confirmed Cancer X with TEST protein expression.')
        self.assertFalse(self.result()['offline_ready'])
        self.quote('Pathology confirmed Cancer X. TEST gene rearrangement was not detected by FISH.')
        self.assertFalse(self.result()['offline_ready'])
        self.record['entities'][-1]['label'] = 'TEST'
        self.dictionary['logical_targets'].append({'event_type': 'fusion', 'gene_symbol': 'TEST', 'target_key': 'Fusion'})
        self.assertFalse(self.result()['offline_ready'])

    def test_bilingual_reference_mapping_rejects_draft_translation(self):
        self.record['entities'][0]['label'] = 'primary mediastinal Cancer X'
        self.record['fact']['population'] = 'primary mediastinal Cancer X'
        self.quote('Pathology confirmed primary mediastinal Cancer X with OLDTEST V1A mutation.')
        self.dictionary['tumors'][0]['name'] = '癌种甲'
        terms = [{'en': 'Cancer X', 'zh': '癌种甲', 'status': 'reference'}]
        _, package, _ = adapt(self.bundle, self.dictionary, None, self.standard, terms)
        self.assertTrue(package['records'][0]['offline_ready'])
        terms[0]['status'] = 'draft'
        _, package, _ = adapt(self.bundle, self.dictionary, None, self.standard, terms)
        self.assertFalse(package['records'][0]['offline_ready'])

    def test_additional_gene_is_not_discarded_to_pass(self):
        self.standard['symbols']['OTHER'] = {'symbol': 'OTHER', 'gene_id': '3', 'hgnc_id': '4'}
        self.record['entities'].append({'ref': 'extra', 'kind': 'gene', 'label': 'OTHER'})
        self.record['fact']['entity_roles']['extra'] = ['molecular']
        self.assertFalse(self.result()['offline_ready'])

    def test_alias_conflicting_with_official_gene_is_not_reassigned(self):
        self.standard['symbols']['OLDTEST'] = {'symbol': 'OLDTEST', 'gene_id': '3', 'hgnc_id': '4'}
        row = self.result()
        # It may create a new key for OLDTEST, but must never point to TEST.
        for conclusion in row['target_payload']['conclusion_inputs']:
            for trigger in conclusion['trigger_inputs']:
                self.assertEqual(trigger['gene_symbol'], 'OLDTEST')


if __name__ == '__main__':
    unittest.main()
