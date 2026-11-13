"""Validate immutable snapshots, stage short batches, then activate atomically."""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from django.db import transaction

from apps.curation.models import (
    Concept,
    DrugMoleculeLink,
    EntityTranslation,
    ResourceAssertion,
    ResourceDataset,
    ResourceEntity,
    SourceRelease,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_file(directory, filename, sha):
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()) or not re.fullmatch(
        r"[a-f0-9]{64}", sha
    ):
        raise ValueError("Invalid snapshot file reference")
    content = path.read_bytes()
    if digest(content) != sha:
        raise ValueError(f"Snapshot checksum mismatch: {filename}")
    return content


def unique_rows(rows, field):
    if not isinstance(rows, list) or any(
        not isinstance(r, dict) or not r.get(field) for r in rows
    ):
        raise ValueError(f"Invalid source rows: {field}")
    result = {str(row[field]): row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate source identifier: {field}")
    return result


def load_snapshot(directory):
    directory = Path(directory)
    raw_manifest = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if manifest.get("state") != "completed":
        raise ValueError("Only a completed source snapshot can be imported")
    data = {}
    if manifest.get("schema") == "chembl-mechanism-snapshot-v1":
        namespace = "CHEMBL_MOA"
        from apps.curation.chembl_source import status_version

        if status_version(manifest["status_before"]) != {
            "version": manifest["version"],
            "release_date": manifest["release_date"],
        }:
            raise ValueError("ChEMBL manifest/source version mismatch")
        # Status payloads can include harmless service metadata; compare release facts.
        if status_version(manifest["status_before"]) != status_version(
            manifest["status_after"]
        ):
            raise ValueError("ChEMBL source version changed")
        for name in ("mechanisms", "molecules", "targets"):
            info = manifest["files"][name]
            data[name] = json.loads(
                checked_file(directory, info["file"], info["sha256"])
            )
            if (
                len(data[name]) != info["count"]
                or info["count"] != manifest["counts"][name]
            ):
                raise ValueError("ChEMBL count mismatch")
        for receipt in manifest["sources"]:
            checked_file(directory, receipt["file"], receipt["sha256"])
    elif manifest.get("schema_version") == "rxclass-epc-snapshot-v1":
        namespace = "RXCLASS_EPC"
        if manifest["versions"] != manifest["versions_after"]:
            raise ValueError("RxClass source version changed")
        if manifest["version"] != manifest["versions"]["FDASPL"]:
            raise ValueError("RxClass manifest/source version mismatch")
        for name in ("classes", "memberships"):
            data[name] = json.loads(
                checked_file(
                    directory, name + ".json", manifest["output_sha256"][name + ".json"]
                )
            )
        counts = manifest["counts"]
        if (
            counts["classes_total"] != counts["classes_completed"]
            or len(data["classes"]) != counts["classes_total"]
            or len(data["memberships"]) != counts["memberships"]
        ):
            raise ValueError("RxClass incomplete capture")
        for receipt in manifest["requests"]:
            if receipt["state"] != "validated":
                raise ValueError("Unvalidated RxClass response")
            checked_file(directory, receipt["raw_file"], receipt["raw_sha256"])
    else:
        raise ValueError("Unsupported reference snapshot schema")
    if any(not data[name] for name in data):
        raise ValueError("Empty source snapshot")
    return namespace, manifest, digest(raw_manifest), data


def prepare_records(namespace, manifest, data):
    entities, assertions = [], []
    if namespace == "RXCLASS_EPC":
        classes = unique_rows(data["classes"], "id")
        for key, row in classes.items():
            if row["class_type"] != "EPC":
                raise ValueError("Non-EPC class")
            entities.append(
                ResourceEntity(
                    kind="class", identifier=key, label=row["name"], attributes=row
                )
            )
        seen = set()
        for row in data["memberships"]:
            if (
                row["class_id"] not in classes
                or not re.fullmatch(r"\d+", row["rxcui"])
                or row["direct"] is not True
                or row["relation_source"] != "FDASPL"
                or row["relation"] != "has_epc"
                or row["tty"] != "IN"
            ):
                raise ValueError("Invalid direct ingredient classification")
            key = row["rxcui"] + ":" + row["class_id"]
            if key in seen:
                raise ValueError("Duplicate ingredient classification")
            seen.add(key)
            assertions.append(
                ResourceAssertion(
                    kind="class_membership",
                    source_record_id=key,
                    subject_identifier="RXNORM:" + row["rxcui"],
                    object_identifier=row["class_id"],
                    predicate=row["relation"],
                    attributes=row,
                )
            )
    else:
        molecules = unique_rows(data["molecules"], "molecule_chembl_id")
        targets = unique_rows(data["targets"], "target_chembl_id")
        mechanisms = unique_rows(data["mechanisms"], "mec_id")
        for kind, records in (("molecule", molecules), ("target", targets)):
            for key, row in records.items():
                if not re.fullmatch(r"CHEMBL\d+", key):
                    raise ValueError("Invalid ChEMBL identifier")
                if kind == "molecule":
                    parent = (row.get("molecule_hierarchy") or {}).get(
                        "parent_chembl_id"
                    )
                    if parent and parent not in molecules:
                        raise ValueError("Missing ChEMBL parent molecule")
                entities.append(
                    ResourceEntity(
                        kind=kind,
                        identifier=key,
                        label=row.get("pref_name") or key,
                        attributes=row,
                    )
                )
        for key, row in mechanisms.items():
            target = row.get("target_chembl_id") or ""
            if row["molecule_chembl_id"] not in molecules or (
                target and target not in targets
            ):
                raise ValueError("Dangling ChEMBL mechanism reference")
            assertions.append(
                ResourceAssertion(
                    kind="mechanism",
                    source_record_id=key,
                    subject_identifier=row["molecule_chembl_id"],
                    object_identifier=target,
                    predicate=row.get("action_type") or "",
                    attributes=row,
                )
            )
    return entities, assertions


def batched_create(model, rows):
    # Separate transactions prevent a reference import holding up literature writes.
    for start in range(0, len(rows), 200):
        model.objects.bulk_create(
            rows[start : start + 200], ignore_conflicts=True, batch_size=100
        )


def molecule_links(dataset, molecule_records, release):
    """Candidate matching only: exact casefolded preferred name / source synonym.

    Punctuation, salts, stereochemistry and parent identities are not simplified.
    Multiple possible molecules are left unresolved rather than taking the first.
    """
    by_name = defaultdict(set)
    for row in molecule_records:
        names = [row.get("pref_name") or ""] + [
            s.get("molecule_synonym") or s.get("synonyms") or ""
            for s in (row.get("molecule_synonyms") or [])
        ]
        for name in names:
            if name.strip():
                by_name[name.strip().casefold()].add(row["molecule_chembl_id"])
    links, ambiguous = [], 0
    for concept in Concept.objects.filter(release=release).iterator(chunk_size=500):
        matches = by_name.get(concept.label.strip().casefold(), set())
        if len(matches) > 1:
            ambiguous += 1
        elif len(matches) == 1:
            links.append(
                DrugMoleculeLink(
                    dataset=dataset,
                    ingredient=concept,
                    molecule_identifier=next(iter(matches)),
                    method="unique_exact_name",
                    status="candidate",
                    provenance={
                        "rxnorm_release": str(release.pk),
                        "rxnorm_version": release.version,
                        "matched_name": concept.label,
                        "normalization": "strip+casefold only",
                        "clinical_identity_verified": False,
                    },
                )
            )
    return links, ambiguous


def import_snapshot(directory):
    namespace, manifest, sha, data = load_snapshot(directory)
    entities, assertions = prepare_records(namespace, manifest, data)
    dataset, _ = ResourceDataset.objects.get_or_create(
        namespace=namespace,
        manifest_sha256=sha,
        defaults={
            "version": manifest["version"],
            "source_url": manifest.get(
                "source_url", "https://lhncbc.nlm.nih.gov/RxNav/APIs/RxClassAPIs.html"
            ),
            "rights": manifest.get("rights", manifest.get("license", {})),
            "manifest": manifest,
        },
    )
    for row in [*entities, *assertions]:
        row.dataset = dataset
    batched_create(ResourceEntity, entities)
    batched_create(ResourceAssertion, assertions)
    if dataset.entities.count() != len(entities) or dataset.assertions.count() != len(
        assertions
    ):
        raise ValueError("Staged resource count mismatch; current version retained")
    counts = {
        **manifest["counts"],
        "entities": len(entities),
        "assertions": len(assertions),
    }
    rxnorm = SourceRelease.objects.filter(namespace="RXNORM", active=True).first()
    if rxnorm and namespace == "CHEMBL_MOA":
        links, ambiguous = molecule_links(dataset, data["molecules"], rxnorm)
        batched_create(DrugMoleculeLink, links)
        counts.update(
            rxnorm_name_candidates=len(links),
            rxnorm_ambiguous_names=ambiguous,
            rxnorm_release=str(rxnorm.pk),
        )
    elif rxnorm:
        ids = {r.subject_identifier for r in assertions}
        matched = Concept.objects.filter(release=rxnorm, identifier__in=ids).count()
        counts.update(
            local_ingredients=matched,
            unmatched_ingredients=len(ids) - matched,
            rxnorm_release=str(rxnorm.pk),
        )
    # Only this small switch holds a write transaction across multiple statements.
    with transaction.atomic():
        ResourceDataset.objects.filter(namespace=namespace, active=True).exclude(
            pk=dataset.pk
        ).update(active=False)
        ResourceDataset.objects.filter(pk=dataset.pk).update(active=True, counts=counts)
    dataset.refresh_from_db()
    return dataset


def import_translations(path, namespace):
    if namespace not in {"RXNORM", "ONCOTREE"}:
        raise ValueError("Unsupported bilingual namespace")
    path = Path(path)
    raw = path.read_bytes()
    manifest_path = path.parent / "display-manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("state") != "completed" or manifest.get("artifacts", {}).get(
            path.name
        ) != digest(raw):
            raise ValueError("Translation display manifest checksum/state mismatch")
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = rows["translations"]
    unique_rows(rows, "identifier")
    release = SourceRelease.objects.get(namespace=namespace, active=True)
    concepts = {c.identifier: c for c in Concept.objects.filter(release=release)}
    records, missing = [], 0
    for row in rows:
        if not row.get("zh") or row.get("status") in {
            "missing",
            "ambiguous",
            "excluded",
        }:
            missing += 1
            continue
        concept = concepts.get(row["identifier"])
        if not concept or concept.label != row["en"]:
            raise ValueError(
                "Translation does not match the current source identity and English label"
            )
        if (
            row.get("namespace", namespace) != namespace
            or row.get("version", release.version) != release.version
            or row.get("release_sha256", release.sha256) != release.sha256
        ):
            raise ValueError("Translation source release mismatch")
        if row["status"] not in {"reference", "project_draft", "confirmed"}:
            raise ValueError("Unknown translation review status")
        source_version = row.get("source_version", row.get("version", release.version))
        provenance = {
            **row,
            "input_sha256": digest(raw),
            "concept_release": str(release.pk),
        }
        records.append(
            EntityTranslation(
                concept=concept,
                language=row.get("language", "zh-Hans"),
                label=row["zh"],
                provider=row.get("provider", row.get("source", "project")),
                status=row["status"],
                source_url=row.get("source_url", ""),
                source_version=source_version,
                match_basis=row.get("match_basis", ""),
                content_sha256=digest(
                    json.dumps(row, ensure_ascii=False, sort_keys=True).encode()
                ),
                provenance=provenance,
            )
        )
    batched_create(EntityTranslation, records)
    return {
        "namespace": namespace,
        "rows": len(records),
        "missing": missing,
        "release": str(release.pk),
        "input_sha256": digest(raw),
    }


def add_translations(rows):
    by_concept = defaultdict(list)
    # Latest record per provider, preserving separately sourced alternatives.
    seen = set()
    for row in EntityTranslation.objects.filter(
        concept_id__in=[r["id"] for r in rows]
    ).order_by("-created_at", "-id"):
        key = (row.concept_id, row.provider)
        if key in seen:
            continue
        seen.add(key)
        by_concept[row.concept_id].append(
            {
                key: getattr(row, key)
                for key in (
                    "label",
                    "language",
                    "provider",
                    "status",
                    "source_url",
                    "source_version",
                    "match_basis",
                )
            }
        )
    priority = {"confirmed": 0, "reference": 1, "project_draft": 2}
    for row in rows:
        row["translations"] = sorted(
            by_concept[row["id"]], key=lambda r: priority[r["status"]]
        )
    return rows


def drug_detail(concept):
    from apps.curation.resource_labels import class_translations

    datasets = list(ResourceDataset.objects.filter(active=True).order_by("namespace"))
    classes, molecules = [], []
    for dataset in datasets:
        if dataset.namespace == "RXCLASS_EPC":
            rows = list(
                dataset.assertions.filter(
                    kind="class_membership", subject_identifier=concept.identifier
                )
            )
            labels = dict(
                dataset.entities.filter(
                    kind="class", identifier__in=[r.object_identifier for r in rows]
                ).values_list("identifier", "label")
            )
            for row in rows:
                classes.append(
                    {
                        "id": row.object_identifier,
                        "label": labels[row.object_identifier],
                        "translations": class_translations(
                            row.object_identifier, labels[row.object_identifier]
                        ),
                        "version": dataset.version,
                        **{
                            k: row.attributes[k]
                            for k in (
                                "relation",
                                "relation_source",
                                "direct",
                                "source_url",
                                "raw_sha256",
                            )
                        },
                    }
                )
        elif dataset.namespace == "CHEMBL_MOA":
            links = list(dataset.drug_links.filter(ingredient=concept))
            ids = [link.molecule_identifier for link in links]
            molecule_rows = {
                r.identifier: r
                for r in dataset.entities.filter(kind="molecule", identifier__in=ids)
            }
            assertions = list(
                dataset.assertions.filter(kind="mechanism", subject_identifier__in=ids)
            )
            target_ids = {
                r.object_identifier for r in assertions if r.object_identifier
            }
            targets = {
                r.identifier: r
                for r in dataset.entities.filter(
                    kind="target", identifier__in=target_ids
                )
            }
            by_molecule = defaultdict(list)
            for assertion in assertions:
                data = assertion.attributes
                target = targets.get(assertion.object_identifier)
                source = data.get("_source", {})
                variant = data.get("variant_sequence")
                by_molecule[assertion.subject_identifier].append(
                    {
                        "id": assertion.source_record_id,
                        "mechanism_of_action": data.get("mechanism_of_action"),
                        "action_type": data.get("action_type"),
                        "direct_interaction": data.get("direct_interaction"),
                        "disease_efficacy": data.get("disease_efficacy"),
                        "variant": {
                            key: variant.get(key)
                            for key in (
                                "accession",
                                "isoform",
                                "mutation",
                                "organism",
                                "tax_id",
                                "version",
                            )
                        }
                        if variant
                        else None,
                        "mechanism_comment": data.get("mechanism_comment"),
                        "selectivity_comment": data.get("selectivity_comment"),
                        "binding_site_comment": data.get("binding_site_comment"),
                        "source_url": source.get("url", dataset.source_url),
                        "raw_sha256": source.get("sha256", ""),
                        "version": dataset.version,
                        "refs": data.get("mechanism_refs", []),
                        "target": {
                            "id": target.identifier,
                            "label": target.label,
                            "organism": target.attributes.get("organism"),
                            "target_type": target.attributes.get("target_type"),
                            "components": target.attributes.get(
                                "target_components", []
                            ),
                        }
                        if target
                        else None,
                    }
                )
            for link in links:
                row = molecule_rows[link.molecule_identifier]
                molecules.append(
                    {
                        "id": row.identifier,
                        "label": row.label,
                        "parent_id": (
                            row.attributes.get("molecule_hierarchy") or {}
                        ).get("parent_chembl_id"),
                        "mapping": {"method": link.method, "status": link.status},
                        "mechanisms": by_molecule[row.identifier],
                    }
                )
    return {
        "identifier": concept.identifier,
        "datasets": [
            {
                "namespace": d.namespace,
                "version": d.version,
                "source_url": d.source_url,
                "rights": d.rights,
                "imported_at": d.imported_at,
                "counts": d.counts,
            }
            for d in datasets
        ],
        "classes": classes,
        "molecules": molecules,
        "notice": "RxClass 为 FDASPL 的直接成分分类。ChEMBL 跨库名称匹配为候选，尚未独立核验；靶点/机制参考不代表具体癌种疗效或批准适应证。未匹配不代表无分类或无作用。本页含美国国家医学图书馆（NLM）提供的数据；NLM 不为本产品背书。",
    }
