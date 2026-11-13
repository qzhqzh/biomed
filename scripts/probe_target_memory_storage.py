"""Exercise real target save/search/match in a new, ephemeral SQLite database.

Run only in the network-disabled, read-only container. Tables are created in a
brand-new :memory: database directly from models; no migration is executed and
no existing database or table is altered.
"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID

from django.db.backends.base.base import BaseDatabaseWrapper

original_ensure_connection = BaseDatabaseWrapper.ensure_connection
import probe_target_isolated_runtime as probe


def only_ephemeral_sqlite(self):
    if self.vendor != "sqlite" or self.settings_dict.get("NAME") != ":memory:":
        raise AssertionError("Only a brand-new in-memory SQLite database is allowed")
    return original_ensure_connection(self)


BaseDatabaseWrapper.ensure_connection = only_ephemeral_sqlite

from biomeddb.models import (
    ClinicalEvidence,
    EvidenceConclusion,
    EvidenceDiseaseScope,
    EvidenceDrug,
    EvidenceTrigger,
    TargetRule,
    TargetRuleCondition,
)
from biomeddb.serializers.evidence_center import ClinicalEvidenceSerializer
from biomeddb.services.evidence_center import (
    collect_facts_for_variant,
    match_evidence_for_variant,
)
from biomeddb.services.logical_target import build_logical_target_index
from biomeddb.views.evidence_center import ClinicalEvidenceViewSet
from console.services.treatment_evidence import TreatmentResolver
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from drf_core.models.review import Review
from drf_somatic_biomeddb.models import Drug, Gene, Tumor, TumorAllele
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter
from reversion.models import Revision, Version


def create_empty_schema():
    if connection.settings_dict["ENGINE"] != "django.db.backends.sqlite3" or connection.settings_dict["NAME"] != ":memory:":
        raise AssertionError("Unexpected database")
    models = (ContentType, get_user_model(), Revision, Version, Review, Drug, Tumor, Gene, TumorAllele,
              TargetRule, TargetRuleCondition, ClinicalEvidence, EvidenceConclusion, EvidenceDrug,
              EvidenceDiseaseScope, EvidenceTrigger)
    with connection.schema_editor() as editor:
        for model in models:
            editor.create_model(model)


def seed_identities(identity):
    drugs, tumors, alleles = probe.article_models(identity)
    Drug.objects.bulk_create(drugs)
    Tumor.objects.bulk_create(tumors)
    genes = {}
    for number, allele in enumerate(alleles, 1):
        symbol = allele.gene.symbol
        if symbol not in genes:
            gene = Gene(id=UUID(int=5000 + number), symbol=symbol, entrez_id=f"fixture-{number}")
            genes[symbol] = gene
    Gene.objects.bulk_create(list(genes.values()))
    for allele in alleles:
        allele.gene = genes[allele.gene.symbol]
    TumorAllele.objects.bulk_create(alleles)
    return alleles


def validate_and_store(item, allele_index):
    payload = item["target_payload"]
    serializer = ClinicalEvidenceSerializer(data=payload, context={"request": NS(user=None)})
    if not serializer.is_valid():
        raise ValueError(f"Real SQLite serializer validation failed: {serializer.errors}")
    bad_ids = {}
    for label in ("drug", "tumor"):
        bad = copy.deepcopy(payload)
        if label == "drug":
            bad["conclusion_inputs"][0]["drug_ids"][0] = str(UUID(int=999999))
        else:
            bad["conclusion_inputs"][0]["disease_scope_inputs"][0]["tumor_id"] = str(UUID(int=999999))
        negative = ClinicalEvidenceSerializer(data=bad, context={"request": NS(user=None)})
        bad_ids[label] = not negative.is_valid()
    if not all(bad_ids.values()):
        raise AssertionError("Actual target serializer accepted an absent entity ID")
    instance = serializer.save()
    instance.refresh_from_db()
    serialized = ClinicalEvidenceSerializer(instance, context={"_logical_target_index": allele_index}).data
    duplicate = ClinicalEvidenceSerializer(data=payload, context={"request": NS(user=None)})
    if duplicate.is_valid():
        raise AssertionError("Identical evidence unexpectedly passed duplicate identity check")
    trigger = serialized["conclusions"][0]["triggers"][0]
    return instance, {"pmid": item["pmid"], "serializer_valid": True, "stored_id": str(instance.id),
                      "stored_active": instance.is_active, "stored_status": instance.review_status,
                      "stored_locked": instance.is_locked, "stored_level": instance.evidence_level,
                      "description_preserved": serialized["evidence_description"] == payload["evidence_description"],
                      "source_preserved": serialized["evidence_source"] == payload["evidence_source"],
                      "target_key": trigger["target_key"], "stored_target_resolution": trigger["target_resolution"],
                      "unknown_drug_id_rejected": bad_ids["drug"], "unknown_tumor_id_rejected": bad_ids["tumor"],
                      "exact_duplicate_rejected": True}


def exercise_consumers(instance, item, number):
    payload, conclusion = item["target_payload"], item["target_payload"]["conclusion_inputs"][0]
    trigger = conclusion["trigger_inputs"][0]
    tumor = Tumor.objects.get(pk=conclusion["disease_scope_inputs"][0]["tumor_id"])
    queryset = ClinicalEvidenceViewSet().get_queryset()
    view = ClinicalEvidenceViewSet()
    request = NS(query_params={"search": trigger["gene_symbol"]})
    searched = SearchFilter().filter_queryset(request, queryset, view)
    if not searched.filter(pk=instance.pk).exists():
        raise AssertionError("Actual management search did not find the inactive evidence")
    variant = probe.make_variant(trigger["gene_symbol"], "p." + trigger["target_key"], tumor.name_cn, number=number)
    before = match_evidence_for_variant(variant)
    if any(row["id"] == str(instance.id) for row in before["matches"]):
        raise AssertionError("Inactive evidence matched a sample")
    # Only in the ephemeral database, simulate the independently reviewed and
    # enabled state to observe the actual target consumer. No grade is assigned.
    ClinicalEvidence.objects.filter(pk=instance.pk).update(review_status="approved", is_active=True, is_locked=False)
    matched = match_evidence_for_variant(variant)
    hits = [row for row in matched["matches"] if row["id"] == str(instance.id)]
    if len(hits) != 1 or hits[0]["evidence_description"] != payload["evidence_description"]:
        raise AssertionError("Actual target consumer did not return the expected evidence and condition text")
    def count(variant):
        response = match_evidence_for_variant(variant)
        if response["warnings"]:
            raise ValueError(f"Unexpected target matching warning: {response['warnings']}")
        return sum(row["id"] == str(instance.id) for row in response["matches"])

    gene, key = trigger["gene_symbol"], trigger["target_key"]
    wrong_key = "L861R" if gene == "EGFR" else "G12V"
    cases = {
        "wrong_variant": probe.make_variant(gene, "p." + wrong_key, tumor.name_cn, number=number + 10),
        "wrong_gene": probe.make_variant("TP53", "p." + key, tumor.name_cn, number=number + 20),
        "wrong_cancer": probe.make_variant(gene, "p." + key, "其他主癌种", number=number + 30),
        "missing_protein_change": probe.make_variant(gene, "p.?", tumor.name_cn, number=number + 40),
        "annotation_gene_conflict": probe.make_variant(gene, "p." + key, tumor.name_cn,
                                                       annotation_gene="OTHER", number=number + 50),
    }
    negative_counts = {name: count(variant) for name, variant in cases.items()}
    if any(negative_counts[name] for name in ("wrong_variant", "wrong_gene", "wrong_cancer", "missing_protein_change")):
        raise AssertionError(f"Unexpected target negative match: {negative_counts}")
    lines = {line: count(probe.make_variant(gene, "p." + key, tumor.name_cn, therapy_line=line, number=number + offset))
             for line, offset in (("first_line", 60), ("second_line", 61))}
    subtypes = {}
    if gene == "KRAS":
        subtypes = {subtype: count(probe.make_variant(gene, "p." + key, tumor.name_cn, subtype=subtype, number=number + offset))
                    for subtype, offset in (("low_grade_serous", 80), ("other_ovarian_subtype", 81))}
    alternate_name = "肺癌" if gene == "EGFR" else "其他卵巢癌名称"
    alternate_name_count = count(probe.make_variant(gene, "p." + key, alternate_name, number=number + 70))
    if set(lines.values()) != {1} or (subtypes and set(subtypes.values()) != {1}) or alternate_name_count:
        raise AssertionError("Unexpected target context or cancer-name behavior")
    facts = collect_facts_for_variant(variant)
    resolver = TreatmentResolver(variant.sample)
    resolver.facts = facts
    row = {"variant": variant, "variants": [variant], "type": "snv"}
    try:
        resolver.direct_records(row)
    except ValidationError as exc:
        error = str(exc.detail)
    else:
        raise AssertionError("Ungraded treatment evidence unexpectedly entered target report")
    return {"ungraded_pending_management_text_search": True, "inactive_patient_matches": 0,
            "simulated_approved_active_patient_matches": len(hits),
            "description_with_qualifications_returned": True, "ungraded_report_error": error,
            "negative_counts": negative_counts, "synthetic_line_matches": lines,
            "synthetic_ovarian_subtype_matches": subtypes, "different_recorded_main_name": alternate_name,
            "different_recorded_main_name_matches": alternate_name_count,
            "actual_orm_used": True, "clinical_recommendation_made": False}


def main():
    identity = json.loads(Path("/test/identity.json").read_text(encoding="utf-8"))
    preview = json.loads(Path("/test/preview.json").read_text(encoding="utf-8"))
    if preview["submit_ready"] or preview["target_written"] or preview["record_count"] != 2:
        raise ValueError("Expected two inactive local candidates")
    create_empty_schema()
    alleles = seed_identities(identity)
    index = build_logical_target_index(alleles)
    stored = [validate_and_store(item, index) for item in preview["records"]]
    # Search/match each before the next item is simulated active.
    results = []
    for number, (item, (instance, result)) in enumerate(zip(preview["records"], stored, strict=True), 1):
        result["consumers"] = exercise_consumers(instance, item, number * 100)
        results.append(result)
    result = {"version": "biomed.two-target-memory-storage.v1", "results": results,
              "database": "brand-new SQLite :memory:", "tables": "created once from target models without migrations",
              "network": "none", "source_mounts": "read-only", "production_connected": False,
              "production_written": False, "real_target_serializer_create": True,
              "target_models_sha256": hashlib.sha256(Path("/opt/api/biomeddb/models/evidence_center.py").read_bytes()).hexdigest()}
    print("BIOMED_STORAGE_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
