"""Source-bound, inactive diagnostic fusion preview for controlled research."""

import copy
import re

from apps.curation.knowledge_clinical_mapping import DIAGNOSIS
from apps.curation.knowledge_export import _description, _roles, validate_target_payload
from apps.curation.source_fusion_pair_preview import CASE, PAIR, _local_source
from apps.curation.source_linked_facts import evidence_index

VERSION = 'biomed.source-fusion-diagnostic-preview.v2'
QUALIFIERS = re.compile(r'(?:(?:myxoid|metastatic|recurrent|primary|adult)\s+)*', re.IGNORECASE)
OTHER_CASE = re.compile(r'\b(?:another|other|different|previous|prior|second|first)\s+(?:case|patient)\b|'
                        r'\b(?:prior|previous|earlier)\s+(?:reports?|studies|literature)\b', re.IGNORECASE)


def deferred(sample, reason):
    return {'version': VERSION, 'group_id': sample.get('group_id'), 'status': 'deferred', 'reason': reason,
            'target_payload': None, 'automatic_clinical_matching': False, 'target_written': False}


def _disease_target(label, document, dictionary, references):
    matches = []
    for term in references.get('terms', []):
        if term.get('status') != 'reference' or not term.get('en') or not term.get('zh') or not term.get('sources'):
            continue
        for name in [term['en'], *term.get('aliases', [])]:
            match = re.fullmatch(r'(.*?)' + re.escape(name), label, re.IGNORECASE)
            if not match or not QUALIFIERS.fullmatch(match.group(1)):
                continue
            definition = None
            if name.casefold() != term['en'].casefold():
                expression = re.compile(rf'(?<!\w){re.escape(term["en"])}\s*\(\s*{re.escape(name)}\s*\)', re.IGNORECASE)
                for passage in document.get('passages', []):
                    if str(passage.get('version_id') or '').replace('-', '') != str(document.get('fulltext_version_id') or '').replace('-', ''):
                        continue
                    hit = expression.search(str(passage.get('text') or ''))
                    if hit:
                        definition = {'passage_id': passage['id'], 'locator': passage['locator'],
                                      'quote': hit.group(0)}
                        break
                if not definition:
                    continue
            targets = [t for t in dictionary.get('tumors', []) if (t.get('name_cn') or t.get('name')) == term['zh']]
            if len(targets) == 1:
                matches.append({'target_id': targets[0]['id'], 'reference': copy.deepcopy(term),
                                'qualifier': match.group(1).strip(), 'alias_definition': definition})
    if len({str(m['target_id']) for m in matches}) != 1:
        return None
    return matches[0]


def preview(sample, *, dictionary, gene_standard, name_references):
    record, document = sample.get('record') or {}, sample.get('source_document') or {}
    fact, assessment = record.get('fact') or {}, record.get('assessment') or {}
    metadata = document.get('metadata') or {}
    types = metadata.get('publication_types')
    if (fact.get('relation') != 'biomarker_association' or fact.get('treatment') or fact.get('drug_names')
            or assessment.get('execution', {}).get('status') != 'complete'
            or 'original-study' not in str(fact.get('scope') or '') or not isinstance(types, list)
            or 'case reports' not in {str(value).casefold() for value in types}
            or 'review' in {str(value).casefold() for value in types}
            or metadata.get('title') != (record.get('article') or {}).get('title')
            or re.search(r'\b(?:two|three|four|[2-9])\s+(?:cases|patients)\b', str(metadata.get('title') or ''), re.IGNORECASE)):
        return deferred(sample, 'not_complete_original_diagnostic_fact')
    try:
        index = evidence_index(record, document)
    except (ValueError, TypeError, KeyError):
        return deferred(sample, 'source_lineage_invalid')
    aliases = fact.get('evidence')
    if not isinstance(aliases, list) or not aliases or any(not isinstance(a, str) or a not in index for a in aliases):
        return deferred(sample, 'fact_evidence_unresolved')
    try:
        roles = _roles(record)
        entities = {e['ref']: e for e in record.get('entities', []) if isinstance(e, dict) and e.get('ref')}
    except (TypeError, ValueError, KeyError):
        return deferred(sample, 'participant_roles_unresolved')
    if len(entities) != len(record.get('entities', [])):
        return deferred(sample, 'duplicate_or_invalid_participant')
    molecular = [e for e in entities.values() if 'molecular' in roles.get(e['ref'], [])]
    pairs = {(m.group(1), m.group(2)) for entity in molecular
             if (m := PAIR.fullmatch(str(entity.get('label') or '').strip()))}
    if len(pairs) != 1:
        return deferred(sample, 'exact_single_fusion_pair_not_established')
    left, right = next(iter(pairs))
    if left == right or left not in gene_standard.get('symbols', {}) or right not in gene_standard.get('symbols', {}):
        return deferred(sample, 'fusion_partners_not_unique_official_genes')
    pair_entities = [e for e in molecular if PAIR.fullmatch(str(e.get('label') or '').strip())]
    allowed = {left, right, *(str(e['label']) for e in pair_entities)}
    if any(str(e.get('label') or '') not in allowed for e in molecular):
        return deferred(sample, 'additional_molecular_condition_not_resolved')
    if not {e['ref'] for e in pair_entities} & set(fact.get('entity_ids') or []):
        return deferred(sample, 'fusion_not_a_fact_participant')
    results = fact.get('results')
    if not isinstance(results, list) or not results or not DIAGNOSIS.search(str(results)):
        return deferred(sample, 'diagnostic_relationship_not_established')
    pair_text = pair_entities[0]['label']
    support = _local_source(index, aliases, pair_text, require_case=True)
    if not support:
        return deferred(sample, 'case_diagnostic_fusion_not_explicitly_source_bound')
    hit = re.search(re.escape(pair_text), support['quote'], re.IGNORECASE)
    before = support['quote'][:hit.start()]
    case_markers, other_markers = list(CASE.finditer(before)), list(OTHER_CASE.finditer(before))
    if (not case_markers or (other_markers and other_markers[-1].start() > case_markers[-1].start())
            or not DIAGNOSIS.search(support['quote'][max(0, hit.start()-120):hit.end()+120])):
        return deferred(sample, 'case_diagnostic_fusion_not_explicitly_source_bound')
    if not any(isinstance(result, dict) and result.get('passage') == support['alias']
               and DIAGNOSIS.search(str(result)) for result in results):
        return deferred(sample, 'diagnostic_result_not_bound_to_same_passage')
    diseases = [e for e in entities.values() if e.get('kind') == 'disease' and 'disease' in roles.get(e['ref'], [])]
    if len(diseases) != 1 or diseases[0]['ref'] not in (fact.get('entity_ids') or []):
        return deferred(sample, 'one_disease_not_established')
    identity = _disease_target(str(diseases[0].get('label') or ''), document, dictionary, name_references)
    if not identity:
        return deferred(sample, 'disease_reference_or_target_identity_unresolved')
    disease = diseases[0]['label']
    # The retained fact and its cited source must mention either its original
    # label or the same source-defined abbreviation; title matching alone is insufficient.
    name = disease.removeprefix(identity['qualifier']).strip() if identity['qualifier'] else disease
    case_start = other_markers[-1].end() if other_markers else case_markers[0].start()
    case_context = support['quote'][case_start:hit.end()]
    if (not re.search(rf'(?<!\w){re.escape(name)}(?!\w)', case_context, re.IGNORECASE)
            or (identity['qualifier'] and not re.search(rf'(?<!\w){re.escape(identity["qualifier"])}(?!\w)',
                                                         case_context, re.IGNORECASE))
            or re.search(r'\b(?:no|not|without|negative|excluded)\b.{0,45}(?<!\w)'
                         + re.escape(name) + r'(?!\w)', case_context, re.IGNORECASE)):
        return deferred(sample, 'diagnostic_disease_not_present_in_cited_source')
    article = record.get('article') or {}
    source = '; '.join(f"{key.upper()}:{article[key]}" for key in ('pmid', 'doi') if article.get(key))
    source += f"; biomed:source-fusion-diagnostic/{record['group_id']}"
    description = _description(fact) + '\nOrdered fusion: ' + left + '::' + right
    description += '\nSource passage: ' + support['quote']
    payload = {'evidence_description': description, 'evidence_description_en': description,
               'evidence_category': 'diagnostic', 'evidence_source': source, 'evidence_level': '-',
               'evidence_type': '', 'is_active': False, 'comment': f"biomed:source-fusion-diagnostic/{record['group_id']}",
               'conclusion_inputs': [{'potency': '-', 'drug_ids': [],
                                      'disease_scope_inputs': [{'tumor_id': identity['target_id']}],
                                      'trigger_inputs': [{'trigger_type': 'direct', 'event_type': 'fusion',
                                                          'gene_symbol': right, 'target_key': f'{left}::{right}'}]}]}
    validation = validate_target_payload(payload, dictionary)
    if not validation['valid'] or validation.get('unresolved'):
        return {**deferred(sample, 'target_contract_or_ids_unresolved'), 'contract_validation': validation}
    return {'version': VERSION, 'group_id': sample.get('group_id'), 'status': 'inactive_structure_preview',
            'reason': 'Source-bound diagnostic candidate; independent semantic and target runtime validation required.',
            'target_payload': payload, 'contract_validation': validation, 'disease_identity': identity,
            'source': {'pmid': article.get('pmid'), 'version_id': record.get('version_id'),
                       'source_sha256': record.get('source_sha256'), 'diagnosis': support},
            'automatic_clinical_matching': False, 'target_written': False, 'submit_ready': False}
