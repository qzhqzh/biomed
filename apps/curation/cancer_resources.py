"""Versioned cancer classifications and conservative bilingual display labels.

Wikidata labels are reference aliases, not a new assertion of equivalence. An
OncoTree NCI cross-reference alone never suffices: the English name must also
match a Wikidata English label/alias. Missing or conflicting labels stay empty.
This module neither changes the production database nor infers clinical facts.
"""

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSIONS_URL = "https://oncotree.mskcc.org/api/versions"
NCIT_URL = "https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ReadMe.txt"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
ALLOWED_HOSTS = {"oncotree.mskcc.org", "evs.nci.nih.gov", "query.wikidata.org"}
INTERVAL = 2
BYTES_PER_SECOND = 256 * 1024
MAX_BYTES = 8 * 1024 * 1024
CURRENT_VERSION = "oncotree_2025_10_03"
LANGUAGES = ("zh-hans", "zh", "zh-cn", "zh-hant", "zh-tw")

# Project display translations of the pinned English release, not official
# Chinese OncoTree terms. Only used when no acceptable reference alias exists.
DRAFT_LABELS = {
    "TISSUE": "组织（分类根）",
    "OVARY": "卵巢/输卵管",
    "LYMPH": "淋巴系统",
    "SOFT_TISSUE": "软组织",
    "THYROID": "甲状腺",
    "PLEURA": "胸膜",
    "PANCREAS": "胰腺",
    "BILIARY_TRACT": "胆道",
    "BREAST": "乳腺",
    "HEAD_NECK": "头颈部",
    "EYE": "眼",
    "AMPULLA_OF_VATER": "乏特壶腹",
    "THYMUS": "胸腺",
    "CERVIX": "子宫颈",
    "PNS": "周围神经系统",
    "BOWEL": "肠",
    "BONE": "骨",
    "SKIN": "皮肤",
    "BLADDER": "膀胱/尿路",
    "BRAIN": "中枢神经系统/脑",
    "ADRENAL_GLAND": "肾上腺",
    "PROSTATE": "前列腺",
    "LUNG": "肺",
    "PENIS": "阴茎",
    "UTERUS": "子宫",
    "OTHER": "其他",
    "TESTIS": "睾丸",
    "LIVER": "肝",
    "PERITONEUM": "腹膜",
    "MYELOID": "髓系",
    "VULVA": "外阴/阴道",
    "KIDNEY": "肾",
    "STOMACH": "食管/胃",
    "BRCA": "浸润性乳腺癌",
    "DCIS": "乳腺导管原位癌",
    "LCIS": "乳腺小叶原位癌",
    "COADREAD": "结直肠腺癌",
    "PAAD": "胰腺腺癌",
    "PRAD": "前列腺腺癌",
    "NSCLC": "非小细胞肺癌",
    "LUAD": "肺腺癌",
    "LUSC": "肺鳞状细胞癌",
    "SCLC": "小细胞肺癌",
    "HCC": "肝细胞癌",
    "CHOL": "胆管癌",
    "BLCA": "膀胱尿路上皮癌",
    "UTUC": "上尿路尿路上皮癌",
    "RCC": "肾细胞癌",
    "CCRCC": "肾透明细胞癌",
    "CESC": "宫颈鳞状细胞癌",
    "CEAD": "宫颈腺癌",
    "UCEC": "子宫内膜癌",
    "OVT": "卵巢上皮性肿瘤",
    "EGC": "食管胃腺癌",
    "ESCC": "食管鳞状细胞癌",
    "HNSC": "头颈部鳞状细胞癌",
    "NPC": "鼻咽癌",
    "THPA": "甲状腺乳头状癌",
    "THFO": "甲状腺滤泡癌",
    "THME": "甲状腺髓样癌",
    "THAP": "甲状腺未分化癌",
    "MEL": "黑色素瘤",
    "GIST": "胃肠间质瘤",
    "GBM": "胶质母细胞瘤",
    "DIFG": "弥漫性胶质瘤",
    "MNG": "脑膜瘤",
    "LNM": "淋巴系统肿瘤",
    "MNM": "髓系肿瘤",
    "AML": "急性髓系白血病",
    "DLBCLNOS": "弥漫性大B细胞淋巴瘤，非特指型",
    "CML": "慢性髓系白血病",
    "CUP": "原发灶不明癌",
    "SARCNOS": "肉瘤，非特指型",
    "PANET": "胰腺神经内分泌肿瘤",
    "LNET": "肺神经内分泌肿瘤",
    "PLMESO": "胸膜间皮瘤",
    "PEMESO": "腹膜间皮瘤",
    "AMPCA": "壶腹癌",
    "NBL": "神经母细胞瘤",
    "WT": "肾母细胞瘤",
    "ABL": "急性嗜碱性粒细胞白血病",
    "ACA": "肾上腺皮质腺瘤",
    "ASPS": "腺泡状软组织肉瘤",
    "ATLL": "成人T细胞白血病/淋巴瘤",
    "DFSP": "隆突性皮肤纤维肉瘤",
    "DSRCT": "促结缔组织增生性小圆细胞肿瘤",
    "GMN": "生殖细胞瘤",
    "LPL": "淋巴浆细胞性淋巴瘤",
    "MCC": "梅克尔细胞癌",
    "MP": "葡萄胎",
    "MPNST": "恶性周围神经鞘瘤",
    "PGNG": "副神经节瘤",
    "PNET": "原始神经外胚层肿瘤",
    "SEM": "精原细胞瘤",
    "THYM": "胸腺瘤",
    "PMF": "原发性骨髓纤维化",
}
REFERENCE_EXCLUSIONS = {
    "ABL": "来源中文省略急性限定，暂用完整项目译名。",
    "ACA": "来源中文省略肾上腺皮质限定。",
    "ASPS": "来源中文未准确保留腺泡状及肉瘤类型。",
    "ATLL": "来源中文省略淋巴瘤分支。",
    "DFSP": "来源中文省略隆突性限定。",
    "DSRCT": "来源中文将desmoplastic误为发育不良，不能直接展示。",
    "GMN": "来源中文胚组织瘤不适合本项目的生殖细胞瘤显示词。",
    "HCC": "来源中文肝脏上皮细胞肿瘤较hepatocellular carcinoma宽泛。",
    "LPL": "来源中文巨球蛋白血症与淋巴浆细胞性淋巴瘤不等价。",
    "MCC": "来源中文未明确carcinoma类型。",
    "MP": "来源中文妊娠滋养细胞肿瘤与molar pregnancy不等价。",
    "MPNST": "来源中文神经腱鞘与nerve sheath含义不符。",
    "PGNG": "来源中文副交感神经瘤不覆盖paraganglioma的完整含义。",
    "PNET": "来源中文成神经管细胞瘤不等价于primitive neuroectodermal tumor。",
    "SEM": "来源中文精原细胞省略肿瘤含义。",
    "THYM": "来源中文胸腺肿瘤比thymoma宽泛。",
    "PMF": "来源中文省略原发性限定。",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def write_json(path, payload):
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Refusing to replace frozen file {path.name}")
        return
    path.write_bytes(content)


def normalize_name(value):
    # Punctuation and case only: no cancer/carcinoma, organ, or subtype inference.
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def stable_release(versions):
    if not isinstance(versions, list):
        raise TypeError("Invalid OncoTree versions")
    latest = [
        v for v in versions if v.get("api_identifier") == "oncotree_latest_stable"
    ]
    if len(latest) != 1 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", latest[0].get("release_date", "")
    ):
        raise ValueError("Missing unique latest stable OncoTree release")
    version = "oncotree_" + latest[0]["release_date"].replace("-", "_")
    fixed = [v for v in versions if v.get("api_identifier") == version]
    if len(fixed) != 1 or fixed[0].get("release_date") != latest[0]["release_date"]:
        raise ValueError("Latest stable has no matching fixed release")
    return fixed[0]


def flatten_tree(tree):
    if not isinstance(tree, dict) or set(tree) != {"TISSUE"}:
        raise ValueError("Missing unique OncoTree root")
    pending = [("TISSUE", tree["TISSUE"], None, 0)]
    records = {}
    while pending:
        key, node, parent, level = pending.pop()
        if not isinstance(node, dict) or node.get("code") != key or key in records:
            raise ValueError("Invalid or duplicate OncoTree node")
        if (
            node.get("parent") != parent
            or type(node.get("level")) is not int
            or node["level"] != level
        ):
            raise ValueError("OncoTree parent/level mismatch")
        if not isinstance(node.get("name"), str) or not node["name"].strip():
            raise ValueError("OncoTree name missing")
        children = node.get("children")
        xrefs = node.get("externalReferences")
        if not isinstance(children, dict) or not isinstance(xrefs, dict):
            raise TypeError("OncoTree children/xrefs invalid")
        nci = xrefs.get("NCI", [])
        if not isinstance(nci, list) or any(
            not isinstance(x, str) or not re.fullmatch(r"C\d+", x) for x in nci
        ):
            raise ValueError("Invalid NCI reference")
        records[key] = {
            "identifier": f"ONCOTREE:{key}",
            "code": key,
            "en": node["name"],
            "parent": f"ONCOTREE:{parent}" if parent else None,
            "level": level,
            "nci_references": nci,
            "external_references": xrefs,
            "tissue": node.get("tissue"),
            "main_type": node.get("mainType"),
        }
        pending.extend(
            (code, child, key, level + 1) for code, child in children.items()
        )
    return [records[key] for key in sorted(records)]


def wikidata_query(nci_codes):
    if not nci_codes or any(not re.fullmatch(r"C\d+", code) for code in nci_codes):
        raise ValueError("Query requires bounded valid NCI identifiers")
    if len(nci_codes) > 75:
        raise ValueError("Query batch exceeds 75 identifiers")
    values = " ".join(json.dumps(code) for code in sorted(set(nci_codes)))
    return (
        "SELECT DISTINCT ?item ?nci ?english ?zh WHERE { VALUES ?nci { "
        + values
        + " } "
        "?item wdt:P1748 ?nci ; (rdfs:label|skos:altLabel) ?english . "
        'FILTER(LANG(?english) = "en") '
        "OPTIONAL { ?item rdfs:label ?zh . FILTER(LANG(?zh) IN "
        '("zh-hans", "zh", "zh-cn", "zh-hant", "zh-tw")) } }'
    )


def parse_wikidata(payload, requested_codes):
    try:
        rows = payload["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Invalid Wikidata SPARQL response") from exc
    if not isinstance(rows, list):
        raise TypeError("Invalid Wikidata bindings")
    entities = {}
    for row in rows:
        try:
            nci, item, english = (
                row["nci"]["value"],
                row["item"]["value"],
                row["english"]["value"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed Wikidata binding") from exc
        if nci not in requested_codes or not re.fullmatch(
            r"https?://www.wikidata.org/entity/Q\d+", item
        ):
            raise ValueError("Unrequested Wikidata identifier")
        if row["english"].get("xml:lang") != "en" or not english:
            raise ValueError("Missing English matching label")
        key = (nci, item.rsplit("/", 1)[-1])
        record = entities.setdefault(
            key, {"nci": nci, "qid": key[1], "english": set(), "chinese": {}}
        )
        record["english"].add(english)
        if "zh" in row:
            lang = row["zh"].get("xml:lang")
            label = row["zh"].get("value")
            if lang not in LANGUAGES or not isinstance(label, str) or not label:
                raise ValueError("Invalid Chinese label")
            if lang in record["chinese"] and record["chinese"][lang] != label:
                raise ValueError("Conflicting Wikidata label in one language")
            record["chinese"][lang] = label
    return [
        {**record, "english": sorted(record["english"])} for record in entities.values()
    ]


def translations(nodes, wikidata, version):
    by_nci = {}
    for item in wikidata:
        by_nci.setdefault(item["nci"], []).append(item)
    rows = []
    for node in nodes:
        candidates = {}
        for nci in node["nci_references"]:
            for item in by_nci.get(nci, []):
                matching = [
                    label
                    for label in item["english"]
                    if normalize_name(label) == normalize_name(node["en"])
                ]
                if matching and item["chinese"]:
                    candidates[item["qid"]] = {**item, "matched_english": matching}
        row = {
            "identifier": node["identifier"],
            "en": node["en"],
            "zh": None,
            "source": "",
            "source_url": "",
            "language": "zh",
            "status": "missing",
            "match_basis": "",
            "version": version,
            "official_source_url": f"https://oncotree.mskcc.org/api/tumorTypes/tree?version={version}",
            "candidates": sorted(candidates.values(), key=lambda v: v["qid"]),
        }
        if len(candidates) == 1:
            item = next(iter(candidates.values()))
            lang = next(lang for lang in LANGUAGES if lang in item["chinese"])
            row.update(
                {
                    "zh": item["chinese"][lang],
                    "language": lang,
                    "status": "reference",
                    "source": "Wikidata",
                    "source_url": f"https://www.wikidata.org/wiki/{item['qid']}",
                    "match_basis": "oncotree_nci_xref_and_exact_normalized_english_label_or_alias",
                    "source_identifier": item["qid"],
                    "nci_reference": item["nci"],
                    "matched_english": item["matched_english"],
                    "rights": "CC0 1.0",
                }
            )
        elif candidates:
            row["status"] = "ambiguous"
        rows.append(row)
    return rows


def display_overlay(output):
    """Freeze a derived presentation layer; keep all raw reference labels intact."""
    output = Path(output)
    if (output / "display-manifest.json").exists():
        prior = json.loads((output / "display-manifest.json").read_text())
        for name, expected in prior["artifacts"].items():
            if sha256((output / name).read_bytes()) != expected:
                raise ValueError("Frozen display artifact hash mismatch")
        return prior
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["oncotree_version"] != CURRENT_VERSION:
        raise ValueError(
            "Project translation glossary requires review for new OncoTree release"
        )
    raw_rows = json.loads((output / "translations.json").read_text())
    rows, excluded = [], []
    for original in raw_rows:
        code = original["identifier"].split(":", 1)[1]
        row = {
            **original,
            "namespace": "ONCOTREE",
            "provider": original["source"],
            "source_version": "wikidata-snapshot-2026-10-25" if original["zh"] else "",
        }
        if code in REFERENCE_EXCLUSIONS and original["zh"]:
            excluded.append(
                {
                    "identifier": original["identifier"],
                    "reference": original,
                    "reason": REFERENCE_EXCLUSIONS[code],
                }
            )
            row.update(
                {
                    "zh": None,
                    "status": "missing",
                    "excluded_reference_reason": REFERENCE_EXCLUSIONS[code],
                }
            )
        if row["zh"] is None and code in DRAFT_LABELS:
            row.update(
                {
                    "zh": DRAFT_LABELS[code],
                    "language": "zh-Hans",
                    "status": "project_draft",
                    "provider": "biomed project translation",
                    "source": "biomed project translation",
                    "source_url": original["official_source_url"],
                    "source_version": "biomed-cancer-labels-2026-10-25",
                    "match_basis": "project_translation_of_pinned_oncotree_english_label",
                    "rights": "Project display translation; English source CC BY 4.0 OncoTree",
                    "note": "项目中文译名草稿，未获OncoTree官方中文认证；保留英文核对。",
                }
            )
        rows.append(row)
    write_json(output / "display-translations.json", rows)
    write_json(output / "excluded-reference-labels.json", excluded)
    result = {
        "schema": "biomed-cancer-display-labels-v1",
        "state": "completed",
        "created_at": now(),
        "source_manifest_sha256": sha256((output / "manifest.json").read_bytes()),
        "counts": {
            "nodes": len(rows),
            "reference_chinese": sum(r["status"] == "reference" for r in rows),
            "project_draft_chinese": sum(r["status"] == "project_draft" for r in rows),
            "missing_chinese": sum(r["zh"] is None for r in rows),
            "excluded_reference_labels": len(excluded),
        },
        "artifacts": {
            name: sha256((output / name).read_bytes())
            for name in ("display-translations.json", "excluded-reference-labels.json")
        },
    }
    write_json(output / "display-manifest.json", result)
    return result


def allowed(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Unapproved cancer resource URL")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    allowed(url)
    time.sleep(INTERVAL)
    request = Request(
        url,
        headers={
            "User-Agent": "biomed-resource-sync/1.0 (https://github.com/qzhqzh/biomed)",
            "Accept": "application/json,text/plain",
        },
    )
    with build_opener(SafeRedirect()).open(request, timeout=60) as response:
        parts, size, start = [], 0, time.monotonic()
        while True:
            chunk = response.read(min(64 * 1024, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("Cancer resource response exceeds limit")
            parts.append(chunk)
            wait = size / BYTES_PER_SECOND - (time.monotonic() - start)
            if wait > 0:
                time.sleep(wait)
        return b"".join(parts)


def capture(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        return json.loads((output / "manifest.json").read_text())
    downloads = []

    def acquire(name, url):
        path = output / name
        metadata = output / (name + ".meta.json")
        if path.exists() and metadata.exists():
            content = path.read_bytes()
            meta = json.loads(metadata.read_text())
            if meta["url"] != url or meta["sha256"] != sha256(content):
                raise ValueError("Frozen resource URL/content mismatch")
        else:
            for attempt in range(3):
                try:
                    content = fetch(url)
                    break
                except (HTTPError, URLError, TimeoutError, OSError):
                    if attempt == 2:
                        raise
                    time.sleep((5, 15)[attempt])
            meta = {
                "url": url,
                "fetched_at": now(),
                "sha256": sha256(content),
                "bytes": len(content),
            }
            path.write_bytes(content)
            write_json(metadata, meta)
        downloads.append({"file": name, **meta})
        return content

    versions = json.loads(acquire("oncotree-versions-before.json", VERSIONS_URL))
    release = stable_release(versions)
    version = release["api_identifier"]
    tree_url = f"https://oncotree.mskcc.org/api/tumorTypes/tree?version={version}"
    nodes = flatten_tree(json.loads(acquire("oncotree-tree.json", tree_url)))
    if len(nodes) < 700:
        raise ValueError("OncoTree unexpectedly small; retain current data")
    readme = acquire("ncit-readme.txt", NCIT_URL).decode()
    ncit = readme.splitlines()[0].strip()
    if not re.fullmatch(r"\d{2}\.\d{2}[a-z]", ncit):
        raise ValueError("NCIt version not recognized")
    codes = sorted({code for node in nodes for code in node["nci_references"]})
    wikidata = []
    for start in range(0, len(codes), 75):
        batch = codes[start : start + 75]
        url = (
            WIKIDATA_URL
            + "?"
            + urlencode({"query": wikidata_query(batch), "format": "json"})
        )
        raw = acquire(f"wikidata-{start // 75 + 1:02d}.json", url)
        wikidata.extend(parse_wikidata(json.loads(raw), set(batch)))
        print(
            json.dumps(
                {
                    "batch": start // 75 + 1,
                    "codes": len(batch),
                    "matched_items": len(wikidata),
                }
            ),
            flush=True,
        )
    after = stable_release(
        json.loads(acquire("oncotree-versions-after.json", VERSIONS_URL))
    )
    if after != release:
        raise ValueError("OncoTree stable version changed during capture")
    rows = translations(nodes, wikidata, version)
    write_json(output / "nodes.json", nodes)
    write_json(output / "wikidata-matches.json", wikidata)
    write_json(output / "translations.json", rows)
    manifest = {
        "schema": "biomed-cancer-resources-v1",
        "state": "completed",
        "completed_at": now(),
        "oncotree_version": version,
        "previous_version": CURRENT_VERSION,
        "update_status": "unchanged"
        if version == CURRENT_VERSION
        else "new_stable_version",
        "ncit_version": ncit,
        "ncit_status": "version_verified_not_imported",
        "counts": {
            "nodes": len(nodes),
            "first_level_groups": sum(n["level"] == 1 for n in nodes),
            "nci_references": len(codes),
            "reference_chinese": sum(r["status"] == "reference" for r in rows),
            "ambiguous_chinese": sum(r["status"] == "ambiguous" for r in rows),
            "missing_chinese": sum(r["zh"] is None for r in rows),
        },
        "limits": {"interval_seconds": INTERVAL, "bytes_per_second": BYTES_PER_SECOND},
        "rights": {
            "oncotree": "CC BY 4.0 — Memorial Sloan Kettering Cancer Center / OncoTree",
            "oncotree_url": "https://github.com/cBioPortal/oncotree/blob/master/LICENSE.md",
            "wikidata": "CC0 1.0 — Wikidata contributors",
            "wikidata_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        },
        "translation_policy": "Reference display aliases only. NCI xref AND exact normalized English label/alias required; no automatic concept equivalence, Chinese is not official OncoTree terminology.",
        "downloads": downloads,
        "artifacts": {
            name: sha256((output / name).read_bytes())
            for name in ("nodes.json", "wikidata-matches.json", "translations.json")
        },
    }
    write_json(output / "manifest.json", manifest)
    return manifest
