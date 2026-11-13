import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.knowledge_observation_report import (
    build_report,
    digest,
    exact_fact_key,
    file_sha,
    markdown,
    observation_directory,
)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fact(group='g1', condition='previous treatment', result='response 50%'):
    return {'group_id': group, 'article': {'pmid': '123'},
            'entities': [{'ref': group + '.e1', 'kind': 'gene', 'label': 'GENE'}],
            'fact': {'relation': 'treatment_outcome', 'population': 'adult lung cancer',
                     'entity_ids': [group + '.e1'], 'entity_roles': {group + '.e1': ['molecular']},
                     'conditions': [condition], 'results': [{'value': result, 'passage': 'P1'}],
                     'evidence': ['P1']}, 'assessment': {'eligibility': {'status': 'eligible'}}}


def export(record):
    return {'group_id': record['group_id'], 'offline_ready': True, 'biomed_companion': record,
            'target_payload': {'evidence_category': 'treatment', 'conclusion_inputs': [{'gene': 'GENE', 'drug': 'A'}]}}


class ObservationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / 'queue.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE batches(id TEXT,state TEXT,attempts INT,seconds REAL,
            created REAL,updated REAL,error TEXT,summary TEXT,next_attempt REAL);
            CREATE TABLE papers(batch_id TEXT,pmid TEXT,state TEXT);''')
        self.obs = self.root / 'observations/night'
        put(self.obs / 'baseline.json', {'started_at': 100, 'baseline_batch_ids': ['old']})
        put(self.root / 'status.json', {'activity': 'running', 'paused': False, 'reason': '',
                                      'updated_at': 190, 'progress': {'stage': 'infer_entities', 'batch': 'running'}})

    def add_batch(self, name, created, records=(), state='completed'):
        self.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?)',
                        (name, state, 1, 60, created, 190, '', '{}', 0))
        self.db.execute('INSERT INTO papers VALUES (?,?,?)', (name, '123', state))
        self.db.commit()
        if state != 'completed':
            return
        final = self.root / 'batches' / name / 'final'
        bundle = {'source_run': name, 'records': list(records)}
        put(final / 'knowledge.json', bundle)
        put(final / 'tumor-api-export.json', {'source_bundle_sha256': digest(bundle),
                                             'records': [export(r) for r in records]})
        put(final / 'summary.json', {'articles': [{'pmid': '123', 'extraction': {'status': 'processed'}}]})
        put(final / 'import-receipt.json', {'bundle_sha256': file_sha(final / 'knowledge.json'),
                                           'result': {'dry_run': False, 'entity_registry': {},
                                                      'candidates': [{'group_id': r['group_id'], 'id': name + r['group_id']} for r in records]}})

    def test_ref_ids_and_citation_aliases_dedup_but_conditions_results_do_not(self):
        a, b = fact('a'), fact('b')
        b['fact']['results'][0]['passage'] = 'P3'
        b['fact']['evidence'] = ['P3']
        self.assertEqual(exact_fact_key(a, export(a)), exact_fact_key(b, export(b)))
        for changed in (fact('b', condition='no previous treatment'), fact('b', result='response 5%')):
            self.assertNotEqual(exact_fact_key(a, export(a)), exact_fact_key(changed, export(changed)))
        literal = copy.deepcopy(a)
        literal['fact']['results'][0]['evidence'] = 'not statistically significant'
        self.assertNotEqual(exact_fact_key(a, export(a)), exact_fact_key(literal, export(literal)))

    def test_repeated_entity_labels_do_not_erase_participant_roles(self):
        a = fact()
        a['entities'].append({'ref': 'other', 'kind': 'gene', 'label': 'GENE'})
        a['fact']['entity_roles']['other'] = ['comparator']
        b = copy.deepcopy(a)
        b['fact']['entity_roles']['g1.e1'] = ['intervention']
        self.assertNotEqual(exact_fact_key(a, export(a)), exact_fact_key(b, export(b)))

    def test_completed_counts_receipts_pending_status_and_history(self):
        self.add_batch('old', 10, [fact('old')])
        self.add_batch('new', 110, [fact('new'), fact('new-result', result='response 40%')])
        self.add_batch('running', 120, state='running')
        before = (self.root / 'queue.sqlite3').read_bytes()
        report = build_report(self.root, 'night', now=200)
        c = report['counts']
        self.assertEqual(c['papers_attempted'], 2)
        self.assertEqual(c['saved_records'], 2)
        self.assertEqual(c['papers_entering_extraction'], 1)
        self.assertEqual(c['offline_ready_records'], 2)
        self.assertEqual(c['exact_historical_duplicates'], 1)
        self.assertEqual(c['new_exact_ready_fact_candidates'], 1)
        self.assertEqual(c['target_groups_pending_semantic_review'], 1)
        self.assertEqual(c['target_groups_seen_in_history'], 1)
        self.assertEqual(report['unfinished_or_failed_batches'][0]['id'], 'running')
        self.assertEqual(report['current']['progress']['stage'], 'infer_entities')
        self.assertEqual(report['current']['status_age_seconds'], 10)
        self.assertIn('未做全批语义核验', markdown(report))
        self.assertIn('不将其当作0产出的成品', markdown(report))
        self.assertEqual((self.root / 'queue.sqlite3').read_bytes(), before)

    def test_bad_receipt_is_reported_as_incomplete_not_counted_as_saved(self):
        self.add_batch('new', 110, [fact()])
        final = self.root / 'batches/new/final'
        put(final / 'import-receipt.json', {'bundle_sha256': 'wrong', 'result': {'dry_run': True}})
        report = build_report(self.root, 'night', now=200)
        self.assertEqual(report['counts']['saved_records'], 0)
        self.assertEqual(report['counts']['completed_batches'], 1)
        self.assertFalse(report['completed_counts_complete'])
        self.assertEqual(report['warnings'][0]['scope'], 'observation_batch')

    def test_external_baseline_exports_join_history(self):
        self.add_batch('new', 110, [fact('new')])
        prior = self.root / 'prior.json'
        put(prior, {'records': [export(fact('old'))]})
        put(self.obs / 'baseline.json', {'started_at': 100, 'baseline_batch_ids': [],
                                         'baseline_exports': [str(prior)]})
        report = build_report(self.root, 'night', now=200)
        self.assertEqual(report['counts']['new_exact_ready_fact_candidates'], 0)
        self.assertTrue(report['history_comparison_complete'])

    def test_completed_excluded_batch_has_zero_knowledge_with_valid_routing(self):
        self.add_batch('excluded', 110, state='ready')
        self.db.execute("UPDATE batches SET state='completed' WHERE id='excluded'")
        self.db.commit()
        put(self.root / 'batches/excluded/batch-result.json',
            {'state': 'no_solid_articles', 'articles': [{'pmid': '123', 'category': 'non_neoplastic'}]})
        report = build_report(self.root, 'night', now=200)
        self.assertTrue(report['completed_counts_complete'])
        self.assertEqual(report['counts']['saved_records'], 0)
        self.assertEqual(report['counts']['papers_entering_extraction'], 0)

    def test_rejects_invalid_baseline_and_observation_traversal(self):
        with self.assertRaises(ValueError):
            observation_directory(self.root, '../outside')
        put(self.obs / 'baseline.json', {'started_at': float('nan'), 'baseline_batch_ids': []})
        with self.assertRaises(ValueError):
            build_report(self.root, 'night')


if __name__ == '__main__':
    unittest.main()
