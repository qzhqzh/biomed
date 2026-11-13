"""Bounded, explainable queue ranking; never a clinical acceptance decision."""
import re
from functools import lru_cache
from pathlib import Path

from apps.curation.knowledge_clinical_mapping import load_gene_alias_standard

VERSION = 'molecular-clinical-priority-v3'
CLINICAL = re.compile(r'\b(?:patients?|clinical|trial|diagnos\w*|patholog\w*|biopsy|cancers?|carcinoma|glioma|sarcoma|nsclc|response|survival)\b', re.IGNORECASE)
THERAPY = re.compile(r'\b(?:trial|treatment|therapy|therapeutic|immunotherapy|chemotherapy|radiotherapy|inhibitor|phase)\b', re.IGNORECASE)
POINT = r'(?:p\.)?[ACDEFGHIKLMNPQRSTVWY][1-9][0-9]{0,5}[ACDEFGHIKLMNPQRSTVWY*]'
EVENT = re.compile(r'(?<!\w)([A-Z][A-Z0-9.-]{1,19})\s+(?:gene\s+)?(' + POINT + r'|rearrangements?|fusions?|amplification|deletion|copy.number loss|genetic alterations?|mutations?)(?!\w)', re.IGNORECASE)
BACKGROUND = re.compile(r'\b(?:(?:recent|previous|prior|earlier|other) studies|studies have (?:shown|reported)|previously reported|known to|other targets)\b', re.IGNORECASE)
EXPERIMENTAL = re.compile(r'\b(?:transfect\w*|plasmids?|xenografts?|engineered|cell lines?|organoids?|mutagenesis|in vitro|flag.tagged)\b', re.IGNORECASE)
DIRECT = re.compile(r'\b(?:biopsy|pathology|sequencing|molecular testing|genomic profiling|FISH|patients? with|confirmed|diagnosed)\b', re.IGNORECASE)
HEMATOLOGIC = re.compile(r'\b(?:lymphoma|leukemia|leukaemia|myeloma|Waldenstr\w*)\b', re.IGNORECASE)
CONFERENCE = re.compile(r'\b(?:symposium|conference proceedings|meeting abstracts|congress abstracts)\b', re.IGNORECASE)


def excluded_hint(text, match):
    """Only demote explicit counterexamples; absence of a hint never excludes a paper."""
    before = text[max(0, match.start()-240):match.start()]
    after = text[match.end():match.end()+120]
    window = before + match.group() + after
    if re.search(r'\b(?:figure|fig|table)\.?\s*$', before[-15:], re.IGNORECASE):
        return 'figure_label'
    if EXPERIMENTAL.search(window):
        return 'experimental_context'
    # Negation is scoped to the clause before this event, not another stain in
    # the paragraph: positive SS18 FISH must survive unrelated negative IHC.
    clause = re.split(r'[.;]', before)[-1]
    negatives = list(re.finditer(r'\b(?:negative (?:for|staining)|no|without)\b', clause, re.IGNORECASE))
    if (negatives and not re.search(r'\b(?:positive|but|whereas)\b',
                                    clause[negatives[-1].end():], re.IGNORECASE)):
        return 'negated_event'
    if re.match(r'\s*(?:was|were|is)?\s*(?:negative|not (?:detected|identified|found))\b', after, re.IGNORECASE):
        return 'negated_event'
    if BACKGROUND.search(clause):
        return 'background_statement'
    return None


@lru_cache(maxsize=1)
def local_genes():
    return load_gene_alias_standard(Path(__file__).resolve().parents[2] / 'data/knowledge/genes.jsonl')


def rank_source(title, passages=(), standard=None):
    standard = standard if standard is not None else local_genes()
    known = set(standard['symbols']) | set(standard.get('aliases', {}))
    hints, demoted = [], []
    sources = [{'locator': 'title', 'text': title}, *list(passages)]
    for row in sources:
        if row.get('locator', '').startswith('back/'):
            continue
        text = row['text']
        if not CLINICAL.search(title + ' ' + text):
            continue
        for match in EVENT.finditer(text):
            gene, event = match.groups()
            if gene.upper() not in known:
                continue
            reason = excluded_hint(text, match)
            if reason:
                demoted.append({'locator': row.get('locator'), 'quote': match.group(), 'reason': reason})
                continue
            specific = bool(re.fullmatch(POINT, event, re.IGNORECASE) or re.search(r'rearrange|fusion|amplif|deletion|copy', event, re.IGNORECASE))
            prominent = row.get('locator') == 'title' or row.get('locator', '').startswith('front/article-meta/abstract/')
            direct = bool(DIRECT.search(text))
            score = 4 if specific and prominent else 3 if specific and direct else 2
            if HEMATOLOGIC.search(text):
                score = min(score, 2)  # Solid-tumor priority; the classifier still decides scope.
            hints.append({'locator': row.get('locator'), 'quote': match.group(), 'specific_event': specific,
                          'prominent': prominent, 'direct_clinical_context': direct, 'score': score})
    priority = max([int(bool(THERAPY.search(title))), *[h['score'] for h in hints]])
    if CONFERENCE.search(title):
        priority = min(priority, 1)
        demoted.append({'locator': 'title', 'quote': title, 'reason': 'conference_collection'})
    return {'version': VERSION, 'priority': priority, 'hints': hints[:8],
            'demoted_hints': demoted[:8],
            'meaning': 'Queue priority only; source relationship and target eligibility still require extraction and adapter checks.'}
