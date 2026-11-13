"""Check duplicate-drug target behavior using synthetic facts in memory only."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID

import probe_target_memory_storage as storage
from biomeddb.serializers.evidence_center import ClinicalEvidenceSerializer
from drf_somatic_biomeddb.models import Drug, Tumor


def main():
    development = json.loads(Path('/test/development.json').read_text(encoding='utf-8'))
    candidates = [row for row in development['records']
                  if row.get('preview', {}).get('status') == 'inactive_structure_preview']
    drugs = json.loads(Path('/test/drugs.json').read_text(encoding='utf-8'))['target_entries']
    if len(candidates) != 1 or len(drugs) != 2:
        raise ValueError('Expected one target payload shape and two drug identities')
    if {row['name_en'].casefold() for row in drugs} != {'lorlatinib'}:
        raise ValueError('Target drug fixtures differ')
    storage.create_empty_schema()
    for row in drugs:
        Drug.objects.create(id=UUID(row['id']), name_cn=row['name_cn'], name_en=row['name_en'])
    payload = copy.deepcopy(candidates[0]['preview']['target_payload'])
    tumor = payload['conclusion_inputs'][0]['disease_scope_inputs'][0]['tumor_id']
    Tumor.objects.create(id=UUID(tumor), name_cn='合成测试癌种', name_en='Synthetic test tumor', is_blood_tumor=False)
    payload.update(evidence_description='Synthetic duplicate-drug fixture A',
                   evidence_description_en='Synthetic duplicate-drug fixture A',
                   evidence_source='synthetic-fixture-only', comment='Synthetic target behavior test only')
    payload['conclusion_inputs'][0]['drug_ids'] = [drugs[0]['id']]

    def serialize(value):
        return ClinicalEvidenceSerializer(data=value, context={'request': NS(user=None)})

    first = serialize(payload)
    if not first.is_valid():
        raise ValueError(f'First synthetic target fixture rejected: {first.errors}')
    instance = first.save()
    other = copy.deepcopy(payload)
    other['conclusion_inputs'][0]['drug_ids'] = [drugs[1]['id']]
    exact = serialize(other)
    exact_rejected = not exact.is_valid()
    other['evidence_description'] = 'Synthetic duplicate-drug fixture B, same intended fact'
    other['evidence_description_en'] = other['evidence_description']
    rewritten = serialize(other)
    rewritten_accepted = rewritten.is_valid()
    both = copy.deepcopy(payload)
    both['evidence_description'] = 'Synthetic fixture with two target drug IDs'
    both['evidence_description_en'] = both['evidence_description']
    both['conclusion_inputs'][0]['drug_ids'] = [row['id'] for row in drugs]
    dual = serialize(both)
    dual_accepted = dual.is_valid()
    if not (exact_rejected and rewritten_accepted and dual_accepted):
        raise AssertionError('Target duplicate behavior differs from the tested expectation')
    saved = ClinicalEvidenceSerializer(instance).data
    stored_drugs = saved['conclusions'][0]['drugs']
    if len(stored_drugs) != 1 or str(stored_drugs[0]['id']) != drugs[0]['id']:
        raise AssertionError('Target did not preserve the single selected drug ID')
    print('BIOMED_DRUG_DUPLICATE_RESULT=' + json.dumps({
        'version': 'target-duplicate-drug-memory-v1', 'database': 'brand-new SQLite :memory:',
        'network': 'none', 'production_connected': False, 'production_written': False,
        'patient_data_used': False, 'synthetic_fact_only': True, 'drug_ids': [r['id'] for r in drugs],
        'one_id_stored': True, 'same_evidence_different_drug_id_rejected_as_duplicate': exact_rejected,
        'rewritten_description_different_drug_id_accepted': rewritten_accepted,
        'two_alias_ids_in_one_conclusion_accepted_as_two_drugs': dual_accepted,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
