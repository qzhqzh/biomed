"""Run actual tumor_api validation and matching with synthetic in-memory inputs.

This file is executed inside the no-network, read-only container launched by
run_target_isolated_validation.py. Database access is disabled; ORM querysets
are replaced by small explicit in-memory collections. No schema is migrated.
"""

import ast
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, "/opt/api")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ["DJANGO_CONFIGURATION"] = "Test"
from configurations import importer

importer.install()
from django.conf import settings
from django.db.backends.base.base import BaseDatabaseWrapper


def forbid_database(*_args, **_kwargs):
    raise AssertionError("Any database connection is forbidden in the isolated runtime probe")


BaseDatabaseWrapper.ensure_connection = forbid_database
tree = ast.parse(Path("/opt/api/config/settings/base.py").read_text(encoding="utf-8"))
base = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Base")
apps = ast.literal_eval(next(node.value for node in base.body if isinstance(node, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "INSTALLED_APPS" for t in node.targets)))
settings.configure(
    BASE_DIR=Path("/opt/api"), SECRET_KEY="isolated-test-key", DEBUG=False, USE_TZ=True, TIME_ZONE="UTC",
    INSTALLED_APPS=apps, AUTH_USER_MODEL="core.User", ATTACHMENT_MODEL="core.Attachment",
    BIOMEDDB_GENE_MODEL="drf_somatic_biomeddb.Gene", CATEGORY_MODEL="drf_core.Category",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    CACHES={name: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": name}
            for name in ("default", "increment_code", "hint", "suggest", "delay_queue", "permission", "gene", "evidence_gene")},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField", ROOT_URLCONF="config.urls",
    STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
              "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    MEDIA_ROOT="/tmp/media", STATIC_URL="/static/", LANGUAGE_CODE="zh-hans", MIDDLEWARE=[], REST_FRAMEWORK={},
)
import django

django.setup()
from biomeddb.models import (
    ClinicalEvidence,
    EvidenceConclusion,
    TargetRule,
)
from biomeddb.serializers.evidence_center import (
    ClinicalEvidenceSerializer,
)
from biomeddb.services.evidence_center import (
    collect_facts_for_variant,
    match_evidence_for_variant,
)
from biomeddb.services.logical_target import (
    canonical_target,
    direct_logical_target_matches,
    logical_target_from_allele,
)
from biomeddb.views.evidence_center import ClinicalEvidenceViewSet
from console.models import Snv
from console.models.report.clinical import get_sorted_clinical_description
from console.services.treatment_evidence import (
    EvidenceRows,
    TreatmentResolver,
)
from django.db.models import Q
from drf_somatic_biomeddb.models import Drug, Tumor, TumorAllele
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter


class Relation:
    def __init__(self, *items):
        self.items = list(items)

    def all(self):
        return self.items


def values_for(item, parts):
    values = [item]
    for part in parts:
        expanded = []
        for value in values:
            value = getattr(value, part, None)
            if isinstance(value, Relation):
                expanded.extend(value.all())
            elif isinstance(value, (list, tuple)):
                expanded.extend(value)
            else:
                expanded.append(value)
        values = expanded
    return values


def compare(item, lookup, expected):
    parts = lookup.split("__")
    op = parts.pop() if parts[-1] in {"in", "contains", "icontains", "isnull"} else "exact"
    values = values_for(item, parts)
    if op == "in":
        return any(value == x or str(value) == str(x) for value in values for x in expected)
    if op == "contains":
        return any(str(expected) in str(value or "") for value in values)
    if op == "icontains":
        return any(str(expected).casefold() in str(value or "").casefold() for value in values)
    if op == "isnull":
        return any((value is None) == expected for value in values)
    return any(value == expected or str(value) == str(expected) for value in values)


def matches(item, condition):
    if isinstance(condition, Q):
        results = [matches(item, child) for child in condition.children]
        result = all(results) if condition.connector == "AND" else any(results)
        return not result if condition.negated else result
    key, expected = condition
    return compare(item, key, expected)


class MemoryQuery(list):
    """Only the queryset operations exercised by these fixtures are provided."""

    def __init__(self, values=(), model=None):
        super().__init__(values)
        self.model = model

    def filter(self, *conditions, **kwargs):
        all_conditions = [*conditions, *kwargs.items()]
        return MemoryQuery([item for item in self if all(matches(item, c) for c in all_conditions)], self.model)

    def get(self, **kwargs):
        rows = self.filter(**kwargs)
        if len(rows) != 1:
            raise self.model.DoesNotExist()
        return rows[0]

    def exists(self):
        return bool(self)

    def all(self):
        return self

    def select_related(self, *_fields):
        return self

    def prefetch_related(self, *_fields):
        return self

    def distinct(self):
        return self

    def order_by(self, *_fields):
        return self


def manager_filter(items, model):
    return lambda *args, **kwargs: MemoryQuery(items, model).filter(*args, **kwargs)


def article_models(identity):
    drugs, tumors, alleles = [], [], []
    for item in identity["drugs"]:
        drugs.append(Drug(id=UUID(item["id"]), name_cn=item["name_cn"], name_en=item["name_en"]))
    for item in identity["tumors"]:
        tumors.append(Tumor(id=UUID(item["id"]), name_cn=item["name_cn"], name_en=item["name_en"],
                            is_blood_tumor=item["is_blood_tumor"]))
    for case in identity["cases"]:
        for item in case["matching_alleles"]:
            allele = TumorAllele(id=UUID(item["id"]), code=item["code"], index=item["index"],
                                 variant=item["variant"], type=item["type"])
            allele._state.fields_cache["gene"] = NS(symbol=item["gene_symbol"], entrez_id="")
            alleles.append(allele)
    return drugs, tumors, alleles


def validate_actual_serializer(payload, drugs, tumors):
    serializer = ClinicalEvidenceSerializer(data=payload)
    conclusion = serializer.fields["conclusion_inputs"].child
    conclusion.fields["drug_ids"].child_relation.queryset = MemoryQuery(drugs, Drug)
    scopes = conclusion.fields["disease_scope_inputs"].child.fields
    scopes["tumor_id"].queryset = MemoryQuery(tumors, Tumor)
    scopes["subtype_id"].queryset = MemoryQuery(tumors, Tumor)
    with patch.object(ClinicalEvidence.objects, "filter", return_value=MemoryQuery([], ClinicalEvidence)):
        valid = serializer.is_valid()
    if not valid:
        raise ValueError(f"Actual target serializer rejected candidate: {serializer.errors}")
    rejected = {}
    for label in ("drug", "tumor"):
        bad = copy.deepcopy(payload)
        if label == "drug":
            bad["conclusion_inputs"][0]["drug_ids"][0] = str(UUID(int=999999))
        else:
            bad["conclusion_inputs"][0]["disease_scope_inputs"][0]["tumor_id"] = str(UUID(int=999999))
        negative = ClinicalEvidenceSerializer(data=bad)
        child = negative.fields["conclusion_inputs"].child
        child.fields["drug_ids"].child_relation.queryset = MemoryQuery(drugs, Drug)
        child.fields["disease_scope_inputs"].child.fields["tumor_id"].queryset = MemoryQuery(tumors, Tumor)
        with patch.object(ClinicalEvidence.objects, "filter", return_value=MemoryQuery([], ClinicalEvidence)):
            rejected[label] = not negative.is_valid()
    if not all(rejected.values()):
        raise AssertionError("Unknown target entity ID unexpectedly passed actual serializer")
    return {"valid": valid, "level": serializer.validated_data["evidence_level"],
            "active": serializer.validated_data["is_active"],
            "potency": serializer.validated_data["conclusion_inputs"][0]["potency"],
            "unknown_drug_id_rejected": rejected["drug"], "unknown_tumor_id_rejected": rejected["tumor"],
            "database_lookups": "in-memory fixtures; duplicate query simulated empty"}


def make_conclusion(item, drug, tumor):
    payload, input_ = item["target_payload"], item["target_payload"]["conclusion_inputs"][0]
    trigger = NS(**input_["trigger_inputs"][0], target_rule_id=None)
    scope = NS(tumor=tumor, tumor_id=tumor.id, subtype=None, subtype_id=None)
    # This is a counterfactual to exercise target consumers; the real preview
    # remains inactive and has not acquired an approved target record.
    evidence = NS(id="sandbox-evidence", pk="sandbox-evidence", code="SANDBOX", evidence_category="treatment",
                  review_status="approved", is_active=True, is_locked=False,
                  evidence_description=payload["evidence_description"], evidence_source=payload["evidence_source"],
                  evidence_level=payload["evidence_level"], evidence_type=payload["evidence_type"],
                  evidence_updated_at="", comment=payload["comment"])
    return NS(id="sandbox-conclusion", pk="sandbox-conclusion", evidence=evidence, potency=input_["potency"],
              disease_scopes=Relation(scope), triggers=Relation(trigger),
              evidence_drugs=Relation(NS(drug=drug, drug_id=drug.id)))


def make_variant(gene, aa_change, tumor_name, *, annotation_gene=None, therapy_line="unspecified", subtype="unspecified", number=1):
    sample = NS(id=f"synthetic-sample-{number}", pk=f"synthetic-sample-{number}", tumor_name=tumor_name,
                is_blood=False, is_blood_tumor=False, p1q19_qc=None, synthetic_therapy_line=therapy_line,
                synthetic_ovarian_subtype=subtype)
    # Deliberately synthetic transcript/nucleotide text; it is not a mapping to a real allele.
    gene_in_annotation = annotation_gene or gene
    snv = Snv(id=UUID(int=number), gene_symbol=gene, level="1", func="exonic",
              canonical_gene_detail=f"{gene_in_annotation}:TEST_TRANSCRIPT:exon1:c.1A>G:{aa_change}")
    snv.sample_id = UUID(int=1000 + number)
    snv._state.fields_cache["sample"] = sample
    return snv


def match_variant(variant, conclusion, alleles):
    with patch.object(TumorAllele.objects, "filter", side_effect=manager_filter(alleles, TumorAllele)), \
         patch.object(TumorAllele.objects, "none", return_value=MemoryQuery([], TumorAllele)), \
         patch.object(TargetRule.objects, "filter", side_effect=manager_filter([], TargetRule)), \
         patch.object(EvidenceConclusion.objects, "filter", side_effect=manager_filter([conclusion], EvidenceConclusion)):
        facts = collect_facts_for_variant(variant)
        result = match_evidence_for_variant(variant)
    if result["warnings"]:
        raise ValueError(f"Target matcher warning: {result['warnings']}")
    return facts, result


def report_records(variant, conclusion, facts):
    resolver = TreatmentResolver(variant.sample)
    resolver.facts, resolver.rules, resolver.conclusions = facts, [], [conclusion]
    row = {"variant": variant, "variants": [variant], "type": "snv"}
    return resolver.direct_records(row)


def test_case(item, identity, drugs, tumors, alleles, number):
    payload, data = item["target_payload"], item["target_payload"]["conclusion_inputs"][0]
    target = data["trigger_inputs"][0]
    drug = next(row for row in drugs if str(row.id) == data["drug_ids"][0])
    tumor = next(row for row in tumors if str(row.id) == data["disease_scope_inputs"][0]["tumor_id"])
    result = {"pmid": item["pmid"], "gene": target["gene_symbol"], "variant": target["target_key"],
              "serializer": validate_actual_serializer(payload, drugs, tumors)}
    conclusion = make_conclusion(item, drug, tumor)
    conclusion.evidence.is_active = False
    conclusion.evidence.review_status = "pending"
    # Exercise the actual management queryset and DRF text search using a
    # synthetic record; no database or target serializer save is involved.
    with patch.object(ClinicalEvidence.objects, "all", return_value=MemoryQuery([conclusion.evidence], ClinicalEvidence)):
        listed = ClinicalEvidenceViewSet().get_queryset()
    searchable = NS(search_fields=("evidence_description", "evidence_source"))
    if not set(searchable.search_fields).issubset(set(ClinicalEvidenceViewSet.search_fields)):
        raise AssertionError("Actual evidence-center view no longer exposes text search")
    searched = SearchFilter().filter_queryset(NS(query_params={"search": target["gene_symbol"]}), listed, searchable)
    if len(searched) != 1:
        raise AssertionError("Inactive ungraded synthetic evidence was not found by management text search")
    result["ungraded_inactive_management_listed"] = len(listed)
    result["ungraded_inactive_text_search_matches"] = len(searched)
    conclusion.evidence.is_active = True
    conclusion.evidence.review_status = "approved"
    proper = make_variant(target["gene_symbol"], "p." + target["target_key"], tumor.name_cn, number=number)
    facts, matched = match_variant(proper, conclusion, alleles)
    expected = canonical_target("mutation", target["gene_symbol"], target["target_key"])
    if len(matched["matches"]) != 1 or matched["matches"][0]["evidence_description"] != payload["evidence_description"]:
        raise AssertionError("Synthetic correctly annotated sample did not match expected target record")
    keys = [fact.logical_target.target_key for fact in facts]
    target_alleles = [a for a in alleles if a.gene.symbol == target["gene_symbol"]]
    allele_only = any(direct_logical_target_matches(expected, logical_target_from_allele(a)) for a in target_alleles)
    self_matches = any(str(fact.allele_id).startswith("snv-self:")
                       and direct_logical_target_matches(expected, fact.logical_target) for fact in facts)
    wrong_alt = "L861R" if target["gene_symbol"] == "EGFR" else "G12V"
    negatives = {
        "wrong_variant": make_variant(target["gene_symbol"], "p." + wrong_alt, tumor.name_cn, number=number + 10),
        "wrong_gene": make_variant("TP53", "p." + target["target_key"], tumor.name_cn, number=number + 20),
        "wrong_cancer": make_variant(target["gene_symbol"], "p." + target["target_key"], "其他主癌种", number=number + 30),
        "missing_protein_change": make_variant(target["gene_symbol"], "p.?", tumor.name_cn, number=number + 40),
        "annotation_gene_conflict": make_variant(target["gene_symbol"], "p." + target["target_key"], tumor.name_cn,
                                                  annotation_gene="OTHER", number=number + 50),
    }
    negative_counts = {name: len(match_variant(variant, conclusion, alleles)[1]["matches"]) for name, variant in negatives.items()}
    # An existing allele may still be selected by its sample gene and AA when
    # annotation_gene differs; isolate this test with no allele lookup results.
    negative_counts["annotation_gene_conflict_no_alleles"] = len(
        match_variant(negatives["annotation_gene_conflict"], conclusion, [])[1]["matches"])
    if any(negative_counts[name] for name in ("wrong_variant", "wrong_gene", "wrong_cancer", "missing_protein_change",
                                               "annotation_gene_conflict_no_alleles")):
        raise AssertionError(f"Unexpected synthetic negative match: {negative_counts}")
    conclusion.evidence.is_active = False
    inactive_count = len(match_variant(proper, conclusion, alleles)[1]["matches"])
    conclusion.evidence.is_active = True
    if inactive_count:
        raise AssertionError("Inactive evidence cannot be matched")
    lines = {}
    for index, line in enumerate(("first_line", "second_line"), start=60):
        variant = make_variant(target["gene_symbol"], "p." + target["target_key"], tumor.name_cn,
                               therapy_line=line, number=number + index)
        lines[line] = len(match_variant(variant, conclusion, alleles)[1]["matches"])
    subtypes = {}
    if target["gene_symbol"] == "KRAS":
        for index, subtype in enumerate(("low_grade_serous", "other_ovarian_subtype"), start=80):
            variant = make_variant(target["gene_symbol"], "p." + target["target_key"], tumor.name_cn,
                                   subtype=subtype, number=number + index)
            subtypes[subtype] = len(match_variant(variant, conclusion, alleles)[1]["matches"])
    other_main_name = "肺癌" if target["gene_symbol"] == "EGFR" else "其他卵巢癌名称"
    alternate_name = make_variant(target["gene_symbol"], "p." + target["target_key"], other_main_name, number=number + 70)
    alternate_name_count = len(match_variant(alternate_name, conclusion, alleles)[1]["matches"])
    try:
        report_records(proper, conclusion, facts)
    except ValidationError as exc:
        ungraded_report = {"status": "validation_error", "message": str(exc.detail)}
    else:
        raise AssertionError("Expected ungraded treatment report to reject '-' level")
    # Only to observe the separate unknown-potency branch; this synthetic level
    # is not assigned to, or recommended for, the extracted evidence.
    conclusion.evidence.evidence_level = "D"
    diagnostic_records = report_records(proper, conclusion, facts)
    grouped_text = get_sorted_clinical_description(EvidenceRows(diagnostic_records), tumor_name=tumor.name_cn)
    conclusion.evidence.evidence_level = payload["evidence_level"]
    if len(diagnostic_records) != 1 or grouped_text:
        raise AssertionError("Unexpected target unknown-potency grouping behavior")
    result.update({"expected_tumor_name": tumor.name_cn, "facts": keys, "matching_allele_count": len(target_alleles),
                   "allele_index_alone_matches": allele_only, "canonical_snv_self_matches": self_matches,
                   "active_approved_simulation_matches": len(matched["matches"]), "inactive_matches": inactive_count,
                   "negative_counts": negative_counts, "different_recorded_main_name": other_main_name,
                   "different_recorded_main_name_matches": alternate_name_count,
                   "synthetic_line_matches": lines, "synthetic_ovarian_subtype_matches": subtypes,
                   "ungraded_treatment_report": ungraded_report,
                   "hypothetical_D_unknown_potency_grouped_text": grouped_text,
                   "hypothetical_D_is_real_grade": False})
    return result


def main():
    identity = json.loads(Path("/test/identity.json").read_text(encoding="utf-8"))
    preview = json.loads(Path("/test/preview.json").read_text(encoding="utf-8"))
    if not identity["transaction_read_only"] or not identity["drugs_all_present"] or not identity["tumors_all_present"]:
        raise ValueError("Target identities were not independently verified")
    if preview["submit_ready"] or preview["target_written"] or preview["record_count"] != 2:
        raise ValueError("Expected two inactive, unsubmitted candidates")
    drugs, tumors, alleles = article_models(identity)
    results = [test_case(item, identity, drugs, tumors, alleles, index * 100)
               for index, item in enumerate(preview["records"], 1)]
    sources = ("biomeddb/services/logical_target.py", "biomeddb/services/evidence_center.py",
               "biomeddb/services/gene_index.py", "biomeddb/services/tumor_allele_match.py",
               "biomeddb/serializers/evidence_center.py", "console/services/treatment_evidence.py",
               "console/models/report/clinical.py", "console/models/variant.py")
    output = {"version": "biomed.two-target-isolated-runtime.v1", "django": django.get_version(), "results": results,
              "source_sha256": {name: hashlib.sha256((Path("/opt/api") / name).read_bytes()).hexdigest() for name in sources},
              "database_connections": "forbidden", "orm_queries": "synthetic in-memory collections",
              "target_written": False, "production_or_patient_data_used": False,
              "real_target_serializer_is_valid_called": True,
              "disclaimer": "Counterfactual approved/active records and synthetic variants; no serializer save or clinical report generation."}
    print("BIOMED_RESULT=" + json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
