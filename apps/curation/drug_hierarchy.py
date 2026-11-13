"""Browse validated EPC → ingredient associations, without inventing class parents."""

from collections import defaultdict

from django.db.models import Q
from django.http import JsonResponse

from apps.curation.models import Concept, ResourceDataset, SourceRelease
from apps.curation.reference_resources import add_translations
from apps.curation.resource_labels import class_translations
from apps.curation.views import api, page, release_data

CONCEPT_FIELDS = (
    "id",
    "identifier",
    "label",
    "definition",
    "synonyms",
    "parents",
    "cross_references",
)


def matches_class(node, query):
    terms = [
        node["identifier"],
        node["label"],
        *[r["label"] for r in node["translations"]],
    ]
    return any(query.casefold() in term.casefold() for term in terms)


def matching_ingredients(concepts, query):
    return concepts.filter(
        Q(identifier__icontains=query)
        | Q(label__icontains=query)
        | Q(synonyms__icontains=query)
        | Q(translations__label__icontains=query)
    ).distinct()


def _dataset(identifier):
    datasets = ResourceDataset.objects.filter(namespace="RXCLASS_EPC")
    if identifier:
        dataset = datasets.filter(pk=identifier).first()
        if not dataset:
            raise ValueError("Unknown EPC dataset")
    else:
        dataset = datasets.filter(active=True).first()
    # Completed files are staged before activation; counts only exist after validation.
    if dataset and (
        dataset.manifest.get("state") != "completed"
        or not dataset.counts.get("entities")
        or "assertions" not in dataset.counts
    ):
        if identifier:
            raise ValueError("EPC dataset has not finished validation")
        return None
    return dataset


@api(["GET"])
def drug_tree(request):
    offset, limit = page(request)
    release_id = request.GET.get("release", "")
    releases = SourceRelease.objects.filter(namespace="RXNORM")
    release = (
        releases.filter(pk=release_id) if release_id else releases.filter(active=True)
    ).first()
    if not release:
        raise ValueError("Unknown RxNorm release")
    query = request.GET.get("q", "").strip()
    class_id = request.GET.get("class_id", "").strip()
    if len(query) > 200 or len(class_id) > 80:
        raise ValueError("Search too long")
    dataset = _dataset(request.GET.get("dataset", ""))
    response = {
        "state": "ready" if dataset else "waiting_dataset",
        "release": release_data(release),
        "dataset": None,
        "kind": "ingredients" if class_id else "classes",
        "class": None,
        "offset": offset,
        "limit": limit,
        "total": 0,
        "results": [],
        "notice": "EPC 药理分类 → RxNorm 成分；同一成分可以属于多个分类。仅展示已收录的直接关联，未收录关联不代表药物没有分类。",
    }
    if not dataset:
        return JsonResponse(response)
    response["dataset"] = {
        "id": dataset.pk,
        "namespace": dataset.namespace,
        "version": dataset.version,
        "source_url": dataset.source_url,
        "rights": dataset.rights,
        "imported_at": dataset.imported_at,
    }
    nodes = list(
        dataset.entities.filter(kind="class")
        .order_by("label", "identifier")
        .values("identifier", "label")
    )
    by_class = defaultdict(set)
    for membership in dataset.assertions.filter(
        kind="class_membership", predicate="has_epc"
    ).values("subject_identifier", "object_identifier"):
        by_class[membership["object_identifier"]].add(membership["subject_identifier"])
    concepts = Concept.objects.filter(release=release)
    local_ids = set(concepts.values_list("identifier", flat=True))
    matching_ids = (
        set(matching_ingredients(concepts, query).values_list("identifier", flat=True))
        if query
        else local_ids
    )
    classified_ids = set()
    for node in nodes:
        members = by_class[node["identifier"]]
        local_members = members & local_ids
        classified_ids.update(local_members)
        node.update(
            translations=class_translations(node["identifier"], node["label"]),
            child_count=len(local_members),
            unavailable_count=len(members - local_ids),
            source_member_count=len(members),
            matching_child_count=len(members & matching_ids),
        )
    response["coverage"] = {
        "classes": len(nodes),
        "classified_ingredients": len(classified_ids),
        "local_ingredients": len(local_ids),
    }
    if class_id:
        node = next((row for row in nodes if row["identifier"] == class_id), None)
        if not node:
            raise ValueError("Class is absent from selected dataset")
        members = concepts.filter(identifier__in=by_class[class_id])
        if query and not matches_class(node, query):
            members = members.filter(identifier__in=matching_ids)
        members = members.order_by("label", "identifier")
        response.update(
            **{"class": node},
            total=members.count(),
            results=add_translations(
                list(members.values(*CONCEPT_FIELDS)[offset : offset + limit])
            ),
        )
    else:
        if query:
            nodes = [
                node
                for node in nodes
                if matches_class(node, query) or node["matching_child_count"]
            ]
        response.update(total=len(nodes), results=nodes[offset : offset + limit])
    return JsonResponse(response)
