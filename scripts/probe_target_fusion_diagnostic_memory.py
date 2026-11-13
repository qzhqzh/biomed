"""Exercise one diagnostic fusion against real target code in temporary SQLite."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID

import probe_target_memory_storage as storage
from biomeddb.models import ClinicalEvidence
from biomeddb.serializers.evidence_center import ClinicalEvidenceSerializer
from biomeddb.services.evidence_center import match_evidence_for_variant
from biomeddb.views.evidence_center import ClinicalEvidenceViewSet
from drf_somatic_biomeddb.models import Drug, Tumor
from probe_target_fusion_memory import Fusion
from rest_framework.filters import SearchFilter


def main():
    development = json.loads(Path('/test/development.json').read_text(encoding='utf-8'))
    candidates = [r for r in development['records']
                  if r.get('preview', {}).get('status') == 'inactive_structure_preview']
    if len(candidates) != 1 or candidates[0]['group_id'] != 'E-42834928-0014':
        raise ValueError('Expected exactly the frozen diagnostic case')
    candidate = candidates[0]['preview']
    payload = candidate['target_payload']
    conclusion = payload['conclusion_inputs'][0]
    if (candidate['submit_ready'] or candidate['automatic_clinical_matching'] or payload['is_active']
            or payload['evidence_category'] != 'diagnostic' or payload['evidence_level'] != '-'
            or conclusion['drug_ids'] or conclusion['potency'] != '-'):
        raise ValueError('Expected a non-drug, inactive, ungraded diagnostic preview')
    storage.create_empty_schema()
    tumor_id = conclusion['disease_scope_inputs'][0]['tumor_id']
    Tumor.objects.create(id=UUID(tumor_id), name_cn='隆突性皮肤纤维肉瘤', name_en='', is_blood_tumor=False)
    synthetic_drug = UUID(int=12345)
    Drug.objects.create(id=synthetic_drug, name_cn='合成测试药物', name_en='Synthetic drug')
    serializer = ClinicalEvidenceSerializer(data=payload, context={'request': NS(user=None)})
    if not serializer.is_valid():
        raise ValueError(f'Target diagnostic serializer rejected preview: {serializer.errors}')
    rejected = {}
    for name in ('unknown_tumor', 'drug_on_diagnostic', 'potency_on_diagnostic'):
        bad = copy.deepcopy(payload)
        if name == 'unknown_tumor':
            bad['conclusion_inputs'][0]['disease_scope_inputs'][0]['tumor_id'] = str(UUID(int=999999))
        elif name == 'drug_on_diagnostic':
            bad['conclusion_inputs'][0]['drug_ids'] = [str(synthetic_drug)]
        else:
            bad['conclusion_inputs'][0]['potency'] = 'S'
        invalid = ClinicalEvidenceSerializer(data=bad, context={'request': NS(user=None)})
        rejected[name] = not invalid.is_valid()
    if not all(rejected.values()):
        raise AssertionError(f'Target accepted invalid diagnostic input: {rejected}')
    instance = serializer.save()
    instance.refresh_from_db()
    if instance.is_active or instance.review_status != 'pending' or not instance.is_locked:
        raise AssertionError('Target did not retain the inactive pending state')
    duplicate = ClinicalEvidenceSerializer(data=payload, context={'request': NS(user=None)})
    if duplicate.is_valid():
        raise AssertionError('Target accepted an exact duplicate')
    query = SearchFilter().filter_queryset(NS(query_params={'search': 'PDGFB'}),
                                           ClinicalEvidenceViewSet().get_queryset(), ClinicalEvidenceViewSet())
    if not query.filter(pk=instance.pk).exists():
        raise AssertionError('Management search did not find the inactive record')

    def count(variant, categories=None):
        result = match_evidence_for_variant(variant, evidence_categories=categories)
        if result['warnings']:
            raise ValueError(f"Unexpected matching warning: {result['warnings']}")
        return sum(row['id'] == str(instance.id) for row in result['matches'])

    exact = Fusion('COL1A1', 'PDGFB', '隆突性皮肤纤维肉瘤', 1)
    if count(exact, ['diagnostic']):
        raise AssertionError('Inactive evidence matched a synthetic case')
    # This state change is confined to the brand-new in-memory database.
    ClinicalEvidence.objects.filter(pk=instance.pk).update(review_status='approved', is_active=True, is_locked=False)
    counts = {'ordered_exact': count(exact, ['diagnostic']),
              'reversed_partners': count(Fusion('PDGFB', 'COL1A1', '隆突性皮肤纤维肉瘤', 2), ['diagnostic']),
              'different_partner': count(Fusion('ETV6', 'PDGFB', '隆突性皮肤纤维肉瘤', 3), ['diagnostic']),
              'wrong_cancer': count(Fusion('COL1A1', 'PDGFB', '其他癌种', 4), ['diagnostic']),
              'missing_partner': count(Fusion('', 'PDGFB', '隆突性皮肤纤维肉瘤', 5), ['diagnostic']),
              'wrong_category': count(exact, ['treatment'])}
    if counts != {'ordered_exact': 1, 'reversed_partners': 0, 'different_partner': 0,
                  'wrong_cancer': 0, 'missing_partner': 0, 'wrong_category': 0}:
        raise AssertionError(f'Unexpected synthetic diagnostic matching: {counts}')
    result = {'version': 'biomed.target-fusion-diagnostic-memory.v1', 'pmid': candidates[0]['pmid'],
              'database': 'brand-new SQLite :memory:', 'network': 'none', 'source_mounts': 'read-only',
              'production_connected': False, 'production_written': False, 'target_migrations_executed': False,
              'serializer_valid': True, 'stored_active': instance.is_active, 'stored_review_status': instance.review_status,
              'unknown_or_treatment_fields_rejected': rejected, 'exact_duplicate_rejected': True,
              'inactive_management_search': True, 'inactive_patient_matches': 0,
              'synthetic_approved_active_match_counts': counts, 'clinical_recommendation_made': False,
              'target_service_sha256': hashlib.sha256(Path('/opt/api/biomeddb/services/evidence_center.py').read_bytes()).hexdigest()}
    print('BIOMED_FUSION_DIAG_RESULT=' + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
