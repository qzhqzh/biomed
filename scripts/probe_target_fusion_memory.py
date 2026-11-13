"""Exercise an ordered-fusion candidate against target code in memory only."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID

import probe_target_memory_storage as storage
from biomeddb.models import ClinicalEvidence
from biomeddb.services.evidence_center import match_evidence_for_variant
from biomeddb.services.logical_target import build_logical_target_index
from biomeddb.views.evidence_center import ClinicalEvidenceViewSet
from drf_somatic_biomeddb.models import Drug, Tumor
from rest_framework.filters import SearchFilter


class Fusion:
    """Synthetic case with target's conventional 5-prime and 3-prime ordering."""

    def __init__(self, left, right, tumor, number):
        self.id = UUID(int=number)
        self.sample_id = UUID(int=number + 100)
        self.gene1_symbol = left
        self.gene2_symbol = right
        self.level = ""
        self.sample = NS(id=self.sample_id, pk=self.sample_id, tumor_name=tumor,
                         is_blood=False, is_blood_tumor=False, p1q19_qc=None)


def main():
    development = json.loads(Path('/test/development.json').read_text(encoding='utf-8'))
    candidates = [r for r in development['records']
                  if r.get('preview', {}).get('status') == 'inactive_structure_preview']
    if len(candidates) != 1 or candidates[0]['group_id'] != 'E-42820355-0021':
        raise ValueError('Expected the frozen one-candidate development result')
    item = {'pmid': candidates[0]['pmid'], 'target_payload': candidates[0]['preview']['target_payload']}
    if item['target_payload']['is_active'] or candidates[0]['preview']['submit_ready']:
        raise ValueError('Expected an inactive preview')

    storage.create_empty_schema()
    conclusion = item['target_payload']['conclusion_inputs'][0]
    Drug.objects.create(id=UUID(conclusion['drug_ids'][0]), name_cn='阿来替尼', name_en='Alectinib')
    Tumor.objects.create(id=UUID(conclusion['disease_scope_inputs'][0]['tumor_id']),
                         name_cn='黑色素瘤', name_en='Melanoma', is_blood_tumor=False)
    instance, serializer = storage.validate_and_store(item, build_logical_target_index([]))
    query = SearchFilter().filter_queryset(NS(query_params={'search': 'ALK'}),
                                           ClinicalEvidenceViewSet().get_queryset(), ClinicalEvidenceViewSet())
    if not query.filter(pk=instance.pk).exists():
        raise AssertionError('Target management search did not find inactive candidate')

    def count(variant):
        result = match_evidence_for_variant(variant)
        if result['warnings']:
            raise ValueError(f"Unexpected target match warning: {result['warnings']}")
        return sum(row['id'] == str(instance.id) for row in result['matches'])

    exact = Fusion('EML4', 'ALK', '黑色素瘤', 1)
    inactive = count(exact)
    if inactive:
        raise AssertionError('Inactive evidence matched a synthetic case')
    # This deliberately changes only the brand-new isolated SQLite database.
    ClinicalEvidence.objects.filter(pk=instance.pk).update(review_status='approved', is_active=True, is_locked=False)
    match_counts = {
        'ordered_exact': count(exact),
        'reversed_partners': count(Fusion('ALK', 'EML4', '黑色素瘤', 2)),
        'different_partner': count(Fusion('KIF5B', 'ALK', '黑色素瘤', 3)),
        'wrong_cancer': count(Fusion('EML4', 'ALK', '其他癌种', 4)),
        'missing_partner': count(Fusion('', 'ALK', '黑色素瘤', 5)),
    }
    if match_counts != {'ordered_exact': 1, 'reversed_partners': 0, 'different_partner': 0,
                        'wrong_cancer': 0, 'missing_partner': 0}:
        raise AssertionError(f'Unexpected fusion matching: {match_counts}')
    result = {'version': 'biomed.target-fusion-memory.v1', 'pmid': item['pmid'],
              'database': 'brand-new SQLite :memory:', 'network': 'none', 'source_mounts': 'read-only',
              'production_connected': False, 'production_written': False, 'target_migrations_executed': False,
              'serializer': serializer, 'inactive_management_search': True, 'inactive_patient_matches': inactive,
              'synthetic_approved_active_match_counts': match_counts,
              'clinical_recommendation_made': False,
              'identity_scope': 'frozen dictionary fixtures, not checked against current live target IDs',
              'target_service_sha256': hashlib.sha256(Path('/opt/api/biomeddb/services/evidence_center.py').read_bytes()).hexdigest()}
    print('BIOMED_FUSION_RESULT=' + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
