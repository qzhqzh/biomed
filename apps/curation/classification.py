"""Versioned, inspectable weak labels. These are not reviewed training labels."""

import hashlib
import json
import re
from pathlib import Path

from apps.curation.models import LiteratureClassification
from apps.curation.network import SourceBlocked

DEFAULT_TAXONOMY = "solid-tumor-rules-v3"
TAXONOMIES = {
    "solid-tumor-rules-v1": "solid-tumor-v1.json",
    "solid-tumor-rules-v2": "solid-tumor-v2.json",
    "solid-tumor-rules-v2.1": "solid-tumor-v2.1.json",
    "solid-tumor-rules-v3": "solid-tumor-v3.json",
}


def taxonomy_snapshot(version=DEFAULT_TAXONOMY):
    if version not in TAXONOMIES:
        raise SourceBlocked("未知的分类词表版本")
    return json.loads(
        (Path(__file__).parent / "vocabularies" / TAXONOMIES[version]).read_text()
    )


def taxonomy_hash(taxonomy):
    return hashlib.sha256(
        json.dumps(taxonomy, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def classify_metadata(metadata, taxonomy):
    """The frozen taxonomy selects the engine so old runs retain v1 semantics."""
    fields = {
        "title": metadata.get("title", ""),
        "abstract": metadata.get("abstract", ""),
        "mesh": " ; ".join(metadata.get("mesh", [])),
        "publication_types": " ; ".join(metadata.get("publication_types", [])),
    }
    contextual = taxonomy.get("engine_version", 1) >= 3
    exclusions = []
    if contextual:
        for exclusion in taxonomy.get("match_exclusions", []):
            for field in exclusion.get("fields", fields):
                for hit in re.finditer(
                    exclusion["pattern"], fields[field], re.IGNORECASE
                ):
                    exclusions.append(
                        {
                            "id": exclusion["id"],
                            "rules": exclusion["rules"],
                            "reason": exclusion["reason"],
                            "field": field,
                            "text": hit.group(),
                            "start": hit.start(),
                            "end": hit.end(),
                        }
                    )
    labels = []
    suppressed = []
    for rule in taxonomy["rules"]:
        if (
            contextual
            and rule.get("requires_context")
            and not any(
                re.search(rule["requires_context"], fields[field], re.IGNORECASE)
                for field in rule.get("context_fields", ("title", "abstract"))
            )
        ):
            continue
        matches = []
        patterns = rule.get("field_patterns")
        if patterns is None:
            patterns = {
                field: r"\b(?:" + rule["pattern"] + r")\b"
                for field in rule.get("fields", fields)
            }
        for field, expression in patterns.items():
            pattern = re.compile(
                expression, 0 if rule.get("case_sensitive") else re.IGNORECASE
            )
            value = fields[field]
            # Search past an excluded molecular name to retain a later real disease.
            hits = pattern.finditer(value) if contextual else [pattern.search(value)]
            for hit in hits:
                if hit is None:
                    continue
                excluded = next(
                    (
                        item
                        for item in exclusions
                        if rule["id"] in item["rules"]
                        and item["field"] == field
                        and item["start"] <= hit.start()
                        and hit.end() <= item["end"]
                    ),
                    None,
                )
                if excluded:
                    suppressed.append({"rule_id": rule["id"], **excluded})
                    continue
                matches.append(
                    {
                        "field": field,
                        "text": hit.group(),
                        "start": hit.start(),
                        "end": hit.end(),
                    }
                )
                break
        if matches:
            labels.append(
                {k: rule[k] for k in ("axis", "id", "label")} | {"matches": matches}
            )
    ids = {label["id"] for label in labels}
    revised = taxonomy.get("engine_version", 1) >= 2
    solid = (
        bool(
            ids
            & (
                {"solid_malignancy", "solid_cell_model"}
                if contextual
                else {"solid_malignancy"}
            )
        )
        if revised
        else any(label["axis"] in ("site", "histology") for label in labels)
    )
    blood = bool(ids & {"hematologic", "hematologic_abbreviation"})
    scope = (
        "mixed_candidate"
        if solid and blood
        else (
            "solid_candidate"
            if solid
            else "hematologic_candidate"
            if blood
            else "uncertain"
        )
    )
    if revised:
        if solid and not blood and ids & {"benign", "precursor"}:
            scope = "solid_boundary_candidate"
        elif not solid and not blood:
            if "benign" in ids:
                scope = "benign_candidate"
            elif "precursor" in ids:
                scope = "precursor_candidate"
    result = {
        "method": "weak_rules",
        "review_status": "unreviewed",
        "scope": scope,
        "labels": labels,
        "missing_axes": sorted(
            {r["axis"] for r in taxonomy["rules"]} - {r["axis"] for r in labels}
        ),
        "input": fields,
        "warning": "规则命中是候选标签，不能视为金标准、原发部位判定或临床证据。",
    }
    if revised:
        result["engine_version"] = taxonomy["engine_version"]
        result["publication_status"] = next(
            (
                status
                for status in (
                    "retracted_publication",
                    "retraction_notice",
                    "expression_of_concern",
                    "correction",
                )
                if status in ids
            ),
            "unmarked",
        )
        if (
            taxonomy.get("publication_status_precedence")
            == "publication_types_before_title"
        ):
            indexed = {
                label["id"]
                for label in labels
                if label["axis"] == "publication_status"
                and any(
                    match["field"] == "publication_types" for match in label["matches"]
                )
            }
            conflicts = [
                label
                for label in labels
                if indexed
                and label["axis"] == "publication_status"
                and label["id"] not in indexed
            ]
            result["publication_status_conflicts"] = conflicts
            result["publication_status_basis"] = (
                "publication_types"
                if indexed
                else "title"
                if result["publication_status"] != "unmarked"
                else "unmarked"
            )
            if indexed:
                result["publication_status"] = (
                    next(iter(indexed))
                    if len(indexed) == 1
                    else "conflicting_source_statuses"
                )
                result["labels"] = [label for label in labels if label not in conflicts]
    if contextual:
        result["suppressed_matches"] = suppressed
        if scope == "uncertain":
            # Triage hints, not a negative diagnosis or an automatic exclusion.
            result["uncertainty_reason"] = (
                "limited_metadata"
                if not fields["abstract"].strip()
                else "oncology_type_unspecified"
                if re.search(
                    r"\b(?:cancers?|tumou?rs?|neoplasms?|oncology)\b",
                    fields["title"],
                    re.IGNORECASE,
                )
                else "no_specific_scope_signal"
            )
    return result


def classify(version, metadata, taxonomy, *, prepared_result=None):
    return LiteratureClassification.objects.get_or_create(
        version=version,
        taxonomy_sha256=taxonomy_hash(taxonomy),
        defaults={
            "taxonomy_version": taxonomy["version"],
            "result": classify_metadata(metadata, taxonomy)
            if prepared_result is None
            else prepared_result,
        },
    )[0]
