"""Repeatable official source adapters; activate a complete snapshot in one transaction."""

import re

from django.db import transaction

from apps.curation.models import Concept, SourceRelease
from apps.curation.network import SourceBlocked, TransientSourceError, fetch, fetch_json
from apps.curation.storage import store_blob

SOURCES = {
    "MONDO": {
        "title": "疾病 · MONDO",
        "rights": "CC BY 4.0 · Monarch Initiative / Mondo",
        "url": "https://mondo.monarchinitiative.org/",
    },
    "RXNORM": {
        "title": "药物成分 · RxNorm",
        "rights": "NLM RxNorm API terms · non-proprietary RxNorm concepts (IN)",
        "url": "https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html",
    },
}


def mondo_terms(content):
    current = None
    terms = []
    for line in content.decode("utf-8").splitlines() + ["[End]"]:
        if line.startswith("["):
            if (
                current
                and current.get("id", "").startswith("MONDO:")
                and current.get("name")
                and not current.get("obsolete")
            ):
                terms.append(current)
            current = (
                {"synonyms": [], "xrefs": [], "parents": []}
                if line == "[Term]"
                else None
            )
        elif current is not None:
            key, _, value = line.partition(": ")
            if key in ("id", "name"):
                current[key] = value
            elif key == "is_obsolete" and value == "true":
                current["obsolete"] = True
            elif key in ("synonym", "def"):
                match = re.match(r'"((?:[^"\\]|\\.)*)"', value)
                if match:
                    label = match.group(1).replace('\\"', '"')
                    if key == "synonym":
                        current["synonyms"].append(label)
                    else:
                        current["definition"] = label
            elif key == "xref":
                current["xrefs"].append(value.split(" ")[0])
            elif key == "is_a":
                current["parents"].append(value.split(" ")[0])
    return terms


def sync_standard(namespace):
    if namespace == "MONDO":
        release = fetch_json(
            "https://api.github.com/repos/monarch-initiative/mondo/releases/latest"
        )
        version = release["tag_name"]
        asset = next((a for a in release["assets"] if a["name"] == "mondo.obo"), None)
        if not asset:
            raise SourceBlocked("MONDO 官方版本没有 mondo.obo 文件")
        url = asset["browser_download_url"]
        content = fetch(url)
        terms = mondo_terms(content)
        if len(terms) < 10000:
            raise SourceBlocked("MONDO 记录数异常，保留当前版本")
    elif namespace == "RXNORM":
        version = fetch_json("https://rxnav.nlm.nih.gov/REST/version.json")["version"]
        url = "https://rxnav.nlm.nih.gov/REST/allconcepts.json?tty=IN"
        content = fetch(url)
        import json

        props = json.loads(content)["minConceptGroup"]["minConcept"]
        terms = [
            {
                "id": f"RXNORM:{p['rxcui']}",
                "name": p["name"],
                "synonyms": [p["synonym"]] if p.get("synonym") else [],
                "xrefs": [],
                "parents": [],
            }
            for p in props
            if p.get("tty") == "IN"
        ]
        if len(terms) < 1000:
            raise SourceBlocked("RxNorm 成分记录数异常，保留当前版本")
        if (
            version
            != fetch_json("https://rxnav.nlm.nih.gov/REST/version.json")["version"]
        ):
            raise TransientSourceError("下载期间 RxNorm 版本发生变化，请重试")
    else:
        raise SourceBlocked("不支持的数据来源")
    digest = store_blob(content)
    if len({term["id"] for term in terms}) != len(terms):
        raise SourceBlocked("来源包含重复标识，未激活新版本")
    snapshot, _ = SourceRelease.objects.get_or_create(
        namespace=namespace,
        sha256=digest,
        defaults={
            "version": version,
            "source_url": url,
            "rights": SOURCES[namespace]["rights"],
            "count": len(terms),
        },
    )
    # Stage bounded batches without exposing partial releases. Interrupted imports resume
    # by the unique (release, identifier) key; the current release is untouched until complete.
    if snapshot.concepts.count() != snapshot.count:
        for start in range(0, len(terms), 500):
            Concept.objects.bulk_create(
                [
                    Concept(
                        release=snapshot,
                        identifier=t["id"],
                        label=t["name"],
                        synonyms=t["synonyms"],
                        cross_references=t["xrefs"],
                        parents=t["parents"],
                        definition=t.get("definition", ""),
                    )
                    for t in terms[start : start + 500]
                ],
                batch_size=500,
                ignore_conflicts=True,
            )
    with transaction.atomic():
        if snapshot.concepts.count() != snapshot.count:
            raise SourceBlocked("来源快照不完整，未激活")
        SourceRelease.objects.filter(namespace=namespace, active=True).exclude(
            pk=snapshot.pk
        ).update(active=False)
        SourceRelease.objects.filter(pk=snapshot.pk).update(active=True)
    return {
        "release_id": str(snapshot.pk),
        "namespace": namespace,
        "version": version,
        "count": snapshot.count,
    }
