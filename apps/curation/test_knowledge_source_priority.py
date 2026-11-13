import unittest
from typing import ClassVar

from apps.curation.knowledge_source_priority import rank_source


class MolecularPriorityTests(unittest.TestCase):
    standard: ClassVar = {'symbols': {g: {} for g in ('KRAS', 'SS18', 'MYC', 'FGFR3', 'RACGAP1', 'BRAF', 'PIK3CA', 'MYD88', 'CXCR4')}, 'aliases': {}}

    def rank(self, title, text=''):
        return rank_source(title, [{'locator': 'body/p:1', 'text': text}], self.standard)['priority']

    def test_specific_molecular_clinical_title_beats_generic_therapy(self):
        self.assertEqual(self.rank('Sotorasib for Lung Cancers with KRAS p.G12C Mutation'), 4)
        self.assertGreater(self.rank('Sarcoma diagnosis', 'Biopsy confirmed sarcoma with SS18 gene rearrangement by FISH.'),
                           self.rank('Sedation therapy during surgery'))

    def test_figure_number_protein_expression_and_background_do_not_become_events(self):
        self.assertEqual(self.rank('Clinical sarcoma study', 'MYC expression was high. See Fig. S3A.'), 0)
        self.assertEqual(self.rank('Clinical sarcoma study', 'Fig. KRAS G12C'), 0)
        result = rank_source('Clinical sarcoma study', [{'locator': 'back/ref:1', 'text': 'KRAS G12C response'}], self.standard)
        self.assertEqual(result['priority'], 0)
        self.assertEqual(self.rank('Cancer signaling study', 'We transfected cancer cell lines with KRAS G12C plasmids.'), 0)

    def test_broad_alteration_is_lower_priority_than_specific_event(self):
        self.assertEqual(self.rank('Clinical trial', 'Patients with FGFR3 genetic alterations received therapy.'), 2)
        self.assertEqual(self.rank('Clinical trial', 'Patients with KRAS G12C received therapy.'), 3)

    def test_real_background_ihc_and_conference_false_positives_are_demoted(self):
        self.assertEqual(self.rank('ARHGAP40 suppresses clear cell renal cell carcinoma progression',
            'Recent studies have shown that the PIK3CA E545K mutation can activate AKT signaling and alter sensitivity to AKT inhibition.'), 0)
        self.assertEqual(self.rank('Renal Neuroendocrine Tumor Mimicking Upper Tract Urothelial Carcinoma',
            'Immunohistochemically, the tumor cells were diffusely positive for synaptophysin and negative for PAX8, CK7, WT1, and BRAF V600E.'), 0)
        self.assertLessEqual(self.rank('Laboratory diagnostics in personalised medicine - 36th Symposium',
            'Other targets: mutations in genes such as IDH1, PIK3CA, KRAS G12C, NTRK, and RET can be treated with specific inhibitors.'), 1)

    def test_actual_abstract_diagnostic_event_outranks_incidental_history(self):
        abstract = rank_source('Aggressive primary mediastinal synovial sarcoma', [{
            'locator': 'front/article-meta/abstract/p:1',
            'text': 'Endobronchial ultrasound-guided biopsy confirmed biphasic synovial sarcoma with an SS18 rearrangement.'}], self.standard)
        self.assertEqual(abstract['priority'], 4)
        self.assertGreater(abstract['priority'], self.rank('Simultaneous immune checkpoint blockade-associated diabetes mellitus',
            'Medical history included malignant melanoma (BRAF V600E+) diagnosed and excised 5 years prior.'))

    def test_negative_stain_does_not_remove_separate_positive_genomic_result(self):
        text = 'Pathology confirmed sarcoma with negative staining for CD99; SS18 gene rearrangement was confirmed by FISH.'
        self.assertEqual(self.rank('Sarcoma diagnosis', text), 3)
        self.assertEqual(self.rank('Sarcoma diagnosis', 'Molecular testing showed SS18 rearrangement was not detected.'), 0)

    def test_hematologic_case_stays_queued_below_clear_solid_case(self):
        text = 'The diagnosis was lymphoplasmacytic lymphoma. Molecular testing demonstrated MYD88 L265P positivity, whereas no CXCR4 mutation was detected.'
        self.assertEqual(self.rank('IgM paraprotein causing electrolyte discrepancies', text), 2)


if __name__ == '__main__':
    unittest.main()
