"""Opt-in molecular diagnostic adapter. Current resident v6 remains unchanged.

The adapter consumes actual extracted facts and exact source spans. It does not
create facts, infer treatment sensitivity, or contact the production target.
"""
import copy
import json
import re
from collections import defaultdict

from apps.curation.knowledge_export import _roles, export_bundle
from apps.curation.knowledge_target_mapping import (
    _nonnegated_name_support,
    load_gene_standard,
    prepare_mappings,
)

VERSION = 'clinical-diagnostic-adapter-v1'
POINT = re.compile(r'(?<!\w)(?:p\.)?([ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*])(?!\w)', re.IGNORECASE)
DIAGNOSIS = re.compile(r'\b(?:diagnos\w*|patholog\w*|defining|confirmed)\b', re.IGNORECASE)


def load_gene_alias_standard(path):
    standard = load_gene_standard(path)
    aliases = defaultdict(set)
    with open(path) as stream:
        for line in stream:
            row = json.loads(line)
            symbol = row.get('symbol', '').upper()
            if symbol not in standard['symbols']:
                continue
            for alias in row.get('synonyms', []):
                aliases[alias.upper()].add(symbol)
    standard['aliases'] = {alias: next(iter(symbols)) for alias, symbols in aliases.items()
                           if len(symbols) == 1 and alias not in standard['symbols']}
    standard['source']['matching'] = 'official_symbol_or_unique_official_synonym; original source spelling preserved'
    return standard


def _events(record, standard):
    roles = _roles(record)
    symbols = standard['symbols']
    aliases = standard.get('aliases', {})
    events = []
    for entity in record.get('entities', []):
        if 'molecular' not in roles.get(entity['ref'], []):
            continue
        label = entity['label']
        words = re.findall(r'(?<!\w)[A-Za-z][A-Za-z0-9.-]+(?!\w)', label)
        genes = {word.upper() if word.upper() in symbols else aliases.get(word.upper()) for word in words}
        genes.discard(None)
        if len(genes) != 1:
            continue
        gene = next(iter(genes))
        points = {m.group(1).upper() for m in POINT.finditer(label)}
        event = key = None
        if len(points) == 1:
            event, key = 'mutation', next(iter(points))
        elif re.fullmatch(r'[\w.-]+\s+(?:gene\s+)?(?:rearrangement|fusion)s?', label, re.IGNORECASE):
            event, key = 'fusion', 'Fusion'
        elif label.upper() == gene:
            # 9B may emit just SS18. Recover no new entity: require the actual
            # relationship model to retain the gene-level event AND an exact
            # occurrence in the cited paragraph; keep the original gene ref.
            event_text = json.dumps(record.get('fact', {}), ensure_ascii=False)
            phrase = rf'\b{re.escape(gene)}\s+(?:gene\s+)?(?:rearrangement|fusion)\b'
            if re.search(phrase, event_text, re.IGNORECASE) and any(
                    a.get('purpose') == 'evidence' and re.search(phrase, a.get('quote', ''), re.IGNORECASE)
                    for a in record.get('anchors', [])):
                event, key = 'fusion', 'Fusion'
        if event:
            events.append({'entity_ref': entity['ref'], 'label': label, 'gene': gene, 'event': event, 'key': key})
    return events


def diagnostic_scope(record, standard):
    fact = record.get('fact', {})
    if (fact.get('relation') != 'biomarker_association' or fact.get('treatment') or fact.get('drug_names')
            or record.get('assessment', {}).get('execution', {}).get('status') != 'complete'):
        return None
    results = json.dumps(fact.get('results', []), ensure_ascii=False)
    if not DIAGNOSIS.search(results):
        return None
    events = _events(record, standard)
    identities = {(e['gene'], e['event'], e['key']) for e in events}
    if len(identities) != 1:
        return None
    event = events[0]
    # Specific genomic condition must survive in the actual model fact.
    fact_text = json.dumps({k: fact.get(k) for k in ('population', 'conditions', 'results')}, ensure_ascii=False)
    if event['key'] != 'Fusion' and not re.search(rf'(?<!\w){re.escape(event["key"])}(?!\w)', fact_text, re.IGNORECASE):
        return None
    support = []
    for anchor in record.get('anchors', []):
        quote = anchor.get('quote', '')
        if anchor.get('purpose') != 'evidence' or not DIAGNOSIS.search(quote):
            continue
        if event['label'].casefold() not in quote.casefold():
            continue
        # Denied/suspected molecular finding is not a positive trigger.
        if re.search(r'\b(?:no|not|negative|absent|without|suspected|possible)\b.{0,40}' + re.escape(event['label']), quote, re.IGNORECASE):
            continue
        event_phrase = re.escape(event['label']) + r'(?:\s+(?:mutation|variant|gene\s+rearrangement|rearrangement|fusion))?'
        if re.search(event_phrase + r'(?:\s+(?:was|were|is|are))?\s+(?:not\s+(?:detected|identified|confirmed)|negative|absent)\b', quote, re.IGNORECASE):
            continue
        support.append({'passage_id': anchor['passage_id'], 'quote': quote})
    if not support:
        return None
    # Do not silently convert additional participating genes or conditions to
    # background. Alias/numbering descriptions of this event remain explicit.
    roles = _roles(record)
    molecular = [e for e in record.get('entities', []) if 'molecular' in roles.get(e['ref'], [])]
    other_genes = set()
    for entity in molecular:
        if entity['kind'] not in {'gene', 'variant'}:
            # Diagnostic immunostains remain in source/conditions, not DNA
            # conditions. A named genomic event is still covered by _events.
            continue
        for word in re.findall(r'(?<!\w)[A-Za-z][A-Za-z0-9.-]+(?!\w)', entity['label']):
            gene = word.upper() if word.upper() in standard['symbols'] else standard.get('aliases', {}).get(word.upper())
            if gene and gene != event['gene']:
                other_genes.add(gene)
    if other_genes:
        return None
    ref = event['entity_ref']
    binding = {'entity_refs': [ref], 'basis': 'Exact source-detected diagnostic molecular event; unique official gene identity; no partner or amino-acid renumbering inferred',
               'source_support': support, 'gene_standard_identity': standard['symbols'][event['gene']],
               'gene_standard_source': standard['source'],
               'input': {'trigger_type': 'direct', 'event_type': event['event'], 'gene_symbol': event['gene'], 'target_key': event['key']}}
    return {'evidence_category': 'diagnostic',
            'trigger_scope': {'kind': 'direct', 'logic': 'single', 'condition_entity_refs': [ref],
                              'non_trigger_entity_refs': [e['ref'] for e in molecular if e['ref'] != ref],
                              'source_support': support,
                              'unstructured_clinical_context': copy.deepcopy(fact.get('conditions', [])),
                              'clinical_context_enforced_by_target': False,
                              'reason': 'Source-supported molecular diagnostic finding; disease, case context and source numbering retained in description and companion.'},
            'treatment_scope': {'kind': 'none', 'entity_refs': [], 'groups': [], 'source_support': support},
            'trigger_bindings': [binding]}


def align_reference_names(record, mapping, dictionary, translations):
    """Use an existing bilingual reference and preserve bounded site qualifiers.

    Generated draft translations and fuzzy substring matches cannot create a
    target identity. These mappings are recorded separately, not added to the
    frozen target dictionary as if the target had provided English aliases.
    """
    changes = []
    roles = _roles(record)
    qualifiers = re.compile(r'(?:(?:primary|mediastinal|biphasic|monophasic|adult|metastatic|recurrent)\s+)*', re.IGNORECASE)
    for entity in record.get('entities', []):
        ref, label = entity['ref'], entity['label']
        if ref in mapping['entity_ids'] or entity['kind'] != 'disease' or 'disease' not in roles.get(ref, []):
            continue
        candidates = []
        for term in translations or []:
            if term.get('status') != 'reference' or not term.get('en') or not term.get('zh'):
                continue
            match = re.fullmatch(r'(.*?)' + re.escape(term['en']), label, re.IGNORECASE)
            if not match or not qualifiers.fullmatch(match[1]):
                continue
            targets = [t for t in dictionary['tumors'] if (t.get('name_cn') or t.get('name')) == term['zh']]
            if len(targets) == 1:
                candidates.append((targets[0]['id'], term, match[1]))
        if len({r[0] for r in candidates}) != 1 or not _nonnegated_name_support(record, label):
            continue
        target, term, prefix = candidates[0]
        mapping['entity_ids'][ref] = target
        mapping['unresolved_entity_refs'] = [r for r in mapping.get('unresolved_entity_refs', []) if r != ref]
        mapping.get('identity_diagnostics', {}).pop(ref, None)
        changes.append({'entity_ref': ref, 'original_label': label, 'target_id': target,
                        'reference': copy.deepcopy(term), 'retained_qualifier': prefix.strip(),
                        'method': 'existing_bilingual_reference_exact_main_disease'})
    if changes:
        mapping['reference_identity_mappings'] = changes
    return changes


def adapt(bundle, dictionary, registry, standard, translations=None):
    mappings, old_report = prepare_mappings(bundle, dictionary, registry, gene_standard=standard)
    changed, names = [], []
    for record in bundle['records']:
        names.extend(align_reference_names(record, mappings[record['group_id']], dictionary, translations))
        scope = diagnostic_scope(record, standard)
        if scope:
            mappings[record['group_id']].update(scope)
            changed.append(record['group_id'])
    package = export_bundle(bundle, dictionary=dictionary, mappings=mappings, gene_standard=standard)
    report = {'version': VERSION, 'diagnostic_scope_changes': changed, 'reference_name_changes': names, 'legacy_mapping_report': old_report,
              'gene_standard_source': standard['source'], 'target_contacted': False, 'target_written': False}
    return mappings, package, report
