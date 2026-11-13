"""Capture CC0 Chinese labels, matching existing RxNorm IN IDs and English names.

The production database is opened in read-only mode for one bounded query. All
network responses and derived files are saved under the selected resource path.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.curation.cancer_resources import (
    LANGUAGES,
    WIKIDATA_URL,
    fetch,
    normalize_name,
    now,
    parse_wikidata,
    sha256,
    write_json,
)

PROJECT_DRUG_LABELS = {
    "osimertinib": "奥希替尼",
    "pembrolizumab": "帕博利珠单抗",
    "crizotinib": "克唑替尼",
    "olaparib": "奥拉帕利",
    "palbociclib": "哌柏西利",
    "lenvatinib": "仑伐替尼",
    "abemaciclib": "阿贝西利",
    "durvalumab": "度伐利尤单抗",
    "ponatinib": "普纳替尼",
    "docetaxel": "多西他赛",
    "lorlatinib": "洛拉替尼",
    "fruquintinib": "呋喹替尼",
}


def capture(output, database):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        return json.loads((output / "manifest.json").read_text())
    source_path = output / "rxnorm-ingredients.json"
    if source_path.exists():
        ingredients = json.loads(source_path.read_text())
    else:
        with sqlite3.connect(
            f"{Path(database).resolve().as_uri()}?mode=ro", uri=True
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            found = connection.execute(
                "SELECT c.identifier, c.label, r.version, r.sha256 FROM curation_concept c "
                "JOIN curation_sourcerelease r ON c.release_id=r.id "
                "WHERE r.namespace='RXNORM' AND r.active=1 ORDER BY c.identifier"
            ).fetchall()
        ingredients = [
            {
                "identifier": row[0],
                "en": row[1],
                "version": row[2],
                "release_sha256": row[3],
            }
            for row in found
        ]
        if len(ingredients) < 10000:
            raise ValueError("Unexpectedly small RxNorm ingredient source")
        write_json(source_path, ingredients)
    downloads, wikidata = [], []
    for language in LANGUAGES:
        legacy_path = output / f"wikidata-rxnorm-{language}.json"
        legacy_metadata = output / f"wikidata-rxnorm-{language}.meta.json"
        legacy = legacy_path.exists() and legacy_metadata.exists()
        projection = (
            "SELECT DISTINCT ?item ?nci ?english ?zh WHERE { "
            if legacy
            else 'SELECT ?item ?nci ?zh (GROUP_CONCAT(DISTINCT ?english; separator="␟") AS ?english_names) WHERE { '
        )
        query = (
            projection
            + (
                f'?item wdt:P3345 ?nci ; rdfs:label ?zh . FILTER(LANG(?zh) = "{language}") '
                '?item (rdfs:label|skos:altLabel) ?english . FILTER(LANG(?english) = "en") }'
            )
            + ("" if legacy else " GROUP BY ?item ?nci ?zh")
        )
        url = WIKIDATA_URL + "?" + urlencode({"query": query, "format": "json"})
        path = (
            legacy_path
            if legacy
            else output / f"wikidata-rxnorm-grouped-{language}.json"
        )
        metadata = (
            legacy_metadata
            if legacy
            else output / f"wikidata-rxnorm-grouped-{language}.meta.json"
        )
        if path.exists() and metadata.exists():
            raw = path.read_bytes()
            meta = json.loads(metadata.read_text())
            if meta["url"] != url or meta["sha256"] != sha256(raw):
                raise ValueError("Frozen source URL/content mismatch")
        else:
            for attempt in range(3):
                try:
                    raw = fetch(url)
                    break
                except (HTTPError, URLError, TimeoutError, OSError):
                    if attempt == 2:
                        raise
                    time.sleep((5, 15)[attempt])
            meta = {
                "url": url,
                "fetched_at": now(),
                "sha256": sha256(raw),
                "bytes": len(raw),
            }
            path.write_bytes(raw)
            write_json(metadata, meta)
        downloads.append({"file": path.name, **meta})
        payload = json.loads(raw)
        if not legacy:
            payload = {
                "results": {
                    "bindings": [
                        {
                            "item": row["item"],
                            "nci": row["nci"],
                            "zh": row["zh"],
                            "english": {
                                "value": english,
                                "xml:lang": "en",
                                "type": "literal",
                            },
                        }
                        for row in payload["results"]["bindings"]
                        for english in row["english_names"]["value"].split("␟")
                    ]
                }
            }
        codes = {row["nci"]["value"] for row in payload["results"]["bindings"]}
        if any(not re.fullmatch(r"\d+", code) for code in codes):
            raise ValueError("Non-numeric RxNorm property returned")
        wikidata.extend(parse_wikidata(payload, codes))
        print(
            json.dumps(
                {
                    "language": language,
                    "rows": len(payload["results"]["bindings"]),
                    "codes": len(codes),
                }
            ),
            flush=True,
        )
    by_rxcui = {}
    for row in wikidata:
        bucket = by_rxcui.setdefault(row["nci"], {})
        existing = bucket.setdefault(
            row["qid"], {**row, "english": set(), "chinese": {}}
        )
        existing["english"].update(row["english"])
        for lang, label in row["chinese"].items():
            if lang in existing["chinese"] and existing["chinese"][lang] != label:
                raise ValueError("Chinese label changed between requests")
            existing["chinese"][lang] = label
    translations, rejected = [], []
    for ingredient in ingredients:
        rxcui = ingredient["identifier"].split(":", 1)[-1]
        exact = []
        for candidate in by_rxcui.get(rxcui, {}).values():
            matched = sorted(
                label
                for label in candidate["english"]
                if normalize_name(label) == normalize_name(ingredient["en"])
            )
            if matched:
                exact.append(
                    {
                        **candidate,
                        "english": sorted(candidate["english"]),
                        "matched_english": matched,
                    }
                )
        row = {
            **ingredient,
            "namespace": "RXNORM",
            "zh": None,
            "status": "missing",
            "provider": "",
            "source_url": "",
            "source_version": "",
            "match_basis": "",
        }
        if len(exact) == 1:
            candidate = exact[0]
            language = next(lang for lang in LANGUAGES if lang in candidate["chinese"])
            row.update(
                {
                    "zh": candidate["chinese"][language],
                    "language": language,
                    "status": "reference",
                    "provider": "Wikidata",
                    "source_url": f"https://www.wikidata.org/wiki/{candidate['qid']}",
                    "source_version": "snapshot-2026-10-25",
                    "match_basis": "rxnorm_id_and_exact_normalized_english_label_or_alias",
                    "source_identifier": candidate["qid"],
                    "matched_english": candidate["matched_english"],
                    "rights": "CC0 1.0",
                }
            )
        elif exact:
            row["status"] = "ambiguous"
            rejected.append(
                {
                    "identifier": row["identifier"],
                    "reason": "multiple_matching_wikidata_items",
                    "candidates": exact,
                }
            )
        elif rxcui in by_rxcui:
            rejected.append(
                {
                    "identifier": row["identifier"],
                    "reason": "id_matches_but_english_name_does_not",
                }
            )
        translations.append(row)
    write_json(output / "translations.json", translations)
    write_json(output / "rejected-candidates.json", rejected)
    manifest = {
        "schema": "biomed-drug-translations-v1",
        "state": "completed",
        "completed_at": now(),
        "counts": {
            "ingredients": len(ingredients),
            "reference_chinese": sum(r["zh"] is not None for r in translations),
            "missing_chinese": sum(r["zh"] is None for r in translations),
            "rejected_candidates": len(rejected),
        },
        "rxnorm_versions": sorted({r["version"] for r in ingredients}),
        "limits": {"interval_seconds": 2, "bytes_per_second": 256 * 1024},
        "rights": "CC0 1.0 — Wikidata contributors",
        "rights_url": "https://www.wikidata.org/wiki/Wikidata:Licensing",
        "policy": "Chinese reference aliases only; both RxNorm ID and English label/alias must match. Never infer approved Chinese INN or salt/parent equivalence.",
        "downloads": downloads,
        "artifacts": {
            name: sha256((output / name).read_bytes())
            for name in (
                "rxnorm-ingredients.json",
                "translations.json",
                "rejected-candidates.json",
            )
        },
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--display-overlay", action="store_true")
    args = parser.parse_args()
    if args.display_overlay:
        print(json.dumps(display_overlay(args.output)["counts"], ensure_ascii=False))
        return
    print(json.dumps(capture(args.output, args.database)["counts"], ensure_ascii=False))


def display_overlay(output):
    output = Path(output)
    if (output / "display-manifest.json").exists():
        previous = json.loads((output / "display-manifest.json").read_text())
        for name, expected in previous["artifacts"].items():
            if sha256((output / name).read_bytes()) != expected:
                raise ValueError("Frozen drug display artifact hash mismatch")
        return previous
    raw = json.loads((output / "translations.json").read_text())
    rows, excluded = [], []
    for original in raw:
        row = dict(original)
        reason = ""
        if row["zh"] and not re.search(r"[\u3400-\u9fff]", row["zh"]):
            reason = "来源虽标记为中文语言，但只有拉丁名或研发代码，不计作中文覆盖。"
            row.update({"zh": None, "status": "missing"})
        draft = PROJECT_DRUG_LABELS.get(row["en"])
        if draft:
            if original["zh"] and not reason:
                reason = "来源中文为品牌或不同译名，本项目使用通用名形式的项目译名草稿；不冒充官方中文确认。"
            rxcui = row["identifier"].split(":", 1)[-1]
            row.update(
                {
                    "zh": draft,
                    "status": "project_draft",
                    "language": "zh-Hans",
                    "provider": "biomed project translation",
                    "source_url": "https://rxnav.nlm.nih.gov/REST/rxcui/"
                    + rxcui
                    + "/properties.json",
                    "source_version": "biomed-drug-labels-2026-10-25",
                    "match_basis": "project_translation_of_exact_rxnorm_ingredient_name",
                    "rights": "Project display translation; English source NLM RxNorm",
                    "note": "项目中文通用名展示草稿，保留英文及RxNorm ID核对，尚非监管机构中文名核验。",
                }
            )
        if reason:
            excluded.append(
                {
                    "identifier": row["identifier"],
                    "reference": original,
                    "reason": reason,
                }
            )
            row["excluded_reference_reason"] = reason
        rows.append(row)
    write_json(output / "display-translations.json", rows)
    write_json(output / "excluded-reference-labels.json", excluded)
    manifest = {
        "schema": "biomed-drug-display-labels-v1",
        "state": "completed",
        "created_at": now(),
        "source_manifest_sha256": sha256((output / "manifest.json").read_bytes()),
        "counts": {
            "ingredients": len(rows),
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
    write_json(output / "display-manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    main()
