"""Repeatable official source adapters; activate a complete snapshot in one transaction."""

import json
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
    "ONCOTREE": {
        "title": "癌种分类 · OncoTree",
        "rights": "CC BY 4.0 · Memorial Sloan Kettering Cancer Center / OncoTree",
        "url": "https://github.com/cBioPortal/oncotree/blob/master/LICENSE.md",
    },
}

# Pin a reviewed official release; never silently consume the development tree.
ONCOTREE_VERSION = "oncotree_2025_10_03"
ONCOTREE_DATE = "2025-10-03"
ONCOTREE_MIN_NODES = 700
ONCOTREE_FETCH = {
    "limit": 2 * 1024 * 1024,
    "bytes_per_second": 256 * 1024,
    "interval": 2,
}


def oncotree_release(versions):
    if not isinstance(versions, list):
        raise SourceBlocked("OncoTree 版本清单格式异常")
    matches = [
        item
        for item in versions
        if isinstance(item, dict) and item.get("api_identifier") == ONCOTREE_VERSION
    ]
    if len(matches) != 1 or matches[0].get("release_date") != ONCOTREE_DATE:
        raise SourceBlocked("OncoTree 固定版本无法确认，保留当前版本")
    return matches[0]


def oncotree_terms(content):
    """Flatten the official tree, retaining its real root and source xrefs.

    Xrefs are references, not asserted equivalences. The immutable source blob
    retains tissue/mainType, colors, history and all other provider attributes.
    """

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SourceBlocked("OncoTree JSON 包含重复键")
            result[key] = value
        return result

    try:
        tree = json.loads(content, object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SourceBlocked("OncoTree JSON 格式异常") from exc
    if not isinstance(tree, dict) or set(tree) != {"TISSUE"}:
        raise SourceBlocked("OncoTree 缺少唯一官方根节点")
    pending = [("TISSUE", tree["TISSUE"], None, 0)]
    seen = set()
    terms = []
    while pending:
        key, node, parent, level = pending.pop()
        if not isinstance(node, dict):
            raise SourceBlocked("OncoTree 节点格式异常")
        code = node.get("code")
        name = node.get("name")
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 71
            or code != key
            or code in seen
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 700
        ):
            raise SourceBlocked("OncoTree 标识或名称异常，或存在重复节点")
        if (
            node.get("parent") != parent
            or type(node.get("level")) is not int
            or node["level"] != level
            or level > 32
        ):
            raise SourceBlocked("OncoTree 父节点或层级不一致，未激活")
        children = node.get("children")
        xrefs = node.get("externalReferences")
        if not isinstance(children, dict) or not isinstance(xrefs, dict):
            raise SourceBlocked("OncoTree 子节点或交叉引用格式异常")
        references = []
        for source, codes in xrefs.items():
            if (
                not source
                or not isinstance(codes, list)
                or any(not isinstance(value, str) or not value for value in codes)
            ):
                raise SourceBlocked("OncoTree 交叉引用内容异常")
            references.extend(f"{source}:{value}" for value in codes)
        seen.add(code)
        terms.append(
            {
                "id": f"ONCOTREE:{code}",
                "name": name,
                "synonyms": [],
                "parents": [f"ONCOTREE:{parent}"] if parent else [],
                "xrefs": references,
            }
        )
        pending.extend(
            (child, value, code, level + 1) for child, value in children.items()
        )
    if len(terms) < ONCOTREE_MIN_NODES:
        raise SourceBlocked("OncoTree 记录数异常，保留当前版本")
    # Matching nesting/parent/level and rejecting repeated codes above also rejects
    # cycles. Keep an explicit closure guard before any database writes.
    identifiers = {term["id"] for term in terms}
    if any(parent not in identifiers for term in terms for parent in term["parents"]):
        raise SourceBlocked("OncoTree 父节点不闭合，未激活")
    return terms


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
    elif namespace == "ONCOTREE":
        versions_url = "https://oncotree.mskcc.org/api/versions"
        before = oncotree_release(fetch_json(versions_url, **ONCOTREE_FETCH))
        version = ONCOTREE_VERSION
        url = f"https://oncotree.mskcc.org/api/tumorTypes/tree?version={version}"
        content = fetch(url, **ONCOTREE_FETCH)
        terms = oncotree_terms(content)
        after = oncotree_release(fetch_json(versions_url, **ONCOTREE_FETCH))
        if before != after:
            raise TransientSourceError("下载期间 OncoTree 版本信息发生变化，请重试")
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
