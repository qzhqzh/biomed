"""Freeze additive, source-bound Chinese display translations; never write a DB.

Only previously untranslated IDs are emitted. English labels, source versions,
existing display manifests and optional release hashes are checked exactly.
This is a reproducible export of reviewed project dictionaries, not an automatic
translation service or a claim of official Chinese terminology.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOCABULARIES = ROOT / "apps/curation/vocabularies"
RESOURCES = Path("/appdata/02_resource/apps/biomed/resources")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_frozen(path, payload):
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing to overwrite frozen artifact: {path}")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
    return digest(raw)


def verify_display(path):
    manifest = read_json(path.parent / "display-manifest.json")
    if manifest.get("state") != "completed" or manifest.get("artifacts", {}).get(
        path.name
    ) != digest(path.read_bytes()):
        raise ValueError(f"Source display checksum/state mismatch: {path}")
    rows = read_json(path)
    result = {}
    for row in rows:
        key = row["identifier"]
        if key in result:
            raise ValueError(f"Duplicate source identifier: {key}")
        result[key] = row
    return result


def gap_reason(english):
    if re.fullmatch(r"[A-Z][A-Z0-9 ._-]*\d[A-Z0-9 ._-]*", english):
        return "development_or_material_code_retained_no_standard_chinese_verified"
    if "allergenic extract" in english or "pollen extract" in english:
        return "allergen_species_or_material_chinese_identity_not_yet_verified"
    if re.search(
        r"\b(PEG|PPG|polymer|copolymer|crosspolymer)\b", english, re.IGNORECASE
    ):
        return "polymer_or_material_specific_name_not_yet_verified"
    return "chinese_name_not_yet_curated_not_a_translation_failure"


def build_drug_dictionary(source, seed, headings, document, destination):
    """Bind exact ingredient names to curated labels and bilingual NHC headings.

    Case is ignored for NHC English headings only. No suffix stripping, salt
    normalization, antibody-drug-conjugate stripping or fuzzy matching is used.
    The ID/English pair is then frozen and rechecked by prepare/importer.
    """
    previous = verify_display(source)
    names = {}
    for row in previous.values():
        key = row["en"].casefold()
        if key in names:
            raise ValueError("Nonunique case-normalized ingredient name")
        names[key] = row
    labels = {}
    for line in seed.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        english, chinese = line.split("\t")
        if english.casefold() in labels:
            raise ValueError("Duplicate drug seed")
        labels[english.casefold()] = {"zh": chinese}
    source_document = read_json(document)
    source_headings = read_json(headings)
    if source_headings["pdf_sha256"] != source_document["sha256"]:
        raise ValueError("NHC heading/PDF metadata digest mismatch")
    verified = {}
    for heading in source_headings["entries"]:
        key = heading["en"].casefold()
        if key in verified and verified[key]["zh"] != heading["zh"]:
            raise ValueError("Conflicting NHC Chinese headings")
        if key in labels and labels[key]["zh"] != heading["zh"]:
            raise ValueError("Project seed differs from NHC bilingual heading")
        verified.setdefault(key, heading)
    for key, heading in verified.items():
        labels[key] = {
            "zh": heading["zh"],
            "source_url": source_document["url"] + f"#page={heading['pdf_page']}",
            "match_basis": "exact_rxnorm_ingredient_name_to_nhc_bilingual_heading_casefold_only",
            "verification": {
                **heading,
                "pdf_sha256": source_document["sha256"],
                "document": "新型抗肿瘤药物临床应用指导原则（2025年版）",
                "scope": "ingredient display name only; no indication or approval inference",
            },
        }
    entries = []
    for key, label in labels.items():
        if key not in names:
            continue
        original = names[key]
        entries.append(
            {
                "identifier": original["identifier"],
                "en": original["en"],
                "status": "project_draft",
                **label,
            }
        )
    versions = {row["version"] for row in previous.values()}
    if len(versions) != 1:
        raise ValueError("Multiple source versions")
    result = {
        "version": "biomed-rxnorm-zh-supplement-2026-10-25-v1",
        "source_version": versions.pop(),
        "source_input_sha256": digest(source.read_bytes()),
        "source_url": "https://www.nlm.nih.gov/research/umls/rxnorm/overview.html",
        "method": "assistant_curated_exact_ingredient_names; NHC bilingual headings where available; no fuzzy/salt/ADC normalization",
        "inputs": {
            "seed_sha256": digest(seed.read_bytes()),
            "nhc_headings_sha256": digest(headings.read_bytes()),
            "nhc_pdf_sha256": source_document["sha256"],
        },
        "entries": sorted(entries, key=lambda row: row["identifier"]),
    }
    write_frozen(destination, result)
    return result


def build_rxclass_dictionary(snapshot, seed, destination):
    manifest = read_json(snapshot / "manifest.json")
    requests = [item for item in manifest["requests"] if item["kind"] == "classes"]
    if len(requests) != 1 or requests[0]["state"] != "validated":
        raise ValueError("No unique validated RxClass class listing")
    request = requests[0]
    raw_path = (snapshot / request["raw_file"]).resolve()
    if not raw_path.is_relative_to(snapshot.resolve()):
        raise ValueError("RxClass raw file escapes snapshot")
    raw = raw_path.read_bytes()
    if digest(raw) != request["raw_sha256"]:
        raise ValueError("RxClass listing checksum mismatch")
    rows = json.loads(raw)["rxclassMinConceptList"]["rxclassMinConcept"]
    labels = {}
    for line in seed.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        identifier, chinese = line.split("\t")
        if identifier in labels or not re.search(r"[\u3400-\u9fff]", chinese):
            raise ValueError("Duplicate or non-Chinese RxClass seed")
        labels[identifier] = chinese
    if len({row["classId"] for row in rows}) != len(rows) or any(
        row["classType"] != "EPC" for row in rows
    ):
        raise ValueError("Invalid or duplicate RxClass source class")
    if set(labels) != {row["classId"] for row in rows}:
        raise ValueError("RxClass seed/source identifiers differ")
    result = {
        "version": "biomed-rxclass-zh-2026-10-25-v1",
        "source": "https://lhncbc.nlm.nih.gov/RxNav/applications/RxClassIntro.html",
        "source_version": manifest["version"],
        "source_sha256": digest(raw),
        "seed_sha256": digest(seed.read_bytes()),
        "method": "assistant_authored_exact_id_and_english_label_translation; no model training; no regulatory Chinese-name verification",
        "status": "project_draft",
        "language": "zh-Hans",
        "note": "项目对冻结英文类别的中文展示草稿。保留原类别语义；不是FDA/NLM官方中文，也不推断用药适应证或类别等价。",
        "labels": {
            row["classId"]: {
                "en": row["className"],
                "zh": labels[row["classId"]],
                "match_basis": "project_translation_of_exact_rxclass_id_and_english_label",
            }
            for row in rows
        },
    }
    write_frozen(destination, result)
    return result


def prepare(namespace, source, supplement, destination):
    previous = verify_display(source)
    additions = read_json(supplement)
    version = additions["version"]
    source_version = additions["source_version"]
    if additions.get("source_input_sha256", digest(source.read_bytes())) != digest(
        source.read_bytes()
    ):
        raise ValueError("Supplement frozen input digest mismatch")
    if not isinstance(version, str) or not isinstance(source_version, str):
        raise TypeError("Translation and source versions must be strings")
    rows, skipped, seen = [], [], set()
    for item in additions["entries"]:
        identifier = item["identifier"]
        if identifier in seen:
            raise ValueError(f"Duplicate supplement identifier: {identifier}")
        seen.add(identifier)
        original = previous.get(identifier)
        if not original or original["en"] != item["en"]:
            raise ValueError(f"Supplement source identity mismatch: {identifier}")
        if original.get("version") != source_version:
            raise ValueError(f"Supplement source version mismatch: {identifier}")
        if not identifier.startswith(namespace + ":"):
            raise ValueError("Wrong namespace")
        if item.get("status", "project_draft") != "project_draft":
            raise ValueError("New project translations must remain project_draft")
        label = item["zh"].strip()
        if not re.search(r"[\u3400-\u9fff]", label):
            raise ValueError(f"Not a Chinese display label: {identifier}")
        if original.get("zh"):
            skipped.append(identifier)
            continue
        row = {
            "namespace": namespace,
            "identifier": identifier,
            "en": original["en"],
            "zh": label,
            "language": "zh-Hans",
            "status": "project_draft",
            "version": source_version,
            "provider": "biomed project translation",
            "source_url": item.get("source_url", additions.get("source_url", "")),
            "source_version": version,
            "match_basis": item.get(
                "match_basis",
                "project_translation_of_exact_source_id_and_english_label",
            ),
            "translation_method": additions.get(
                "method", "assistant_authored_display_translation"
            ),
            "note": "项目中文展示草稿；保留英文及ID。不是官方中文确认，不改变实体身份、分类或临床证据。",
        }
        if original.get("release_sha256"):
            row["release_sha256"] = original["release_sha256"]
        if item.get("review_note"):
            row["review_note"] = item["review_note"]
        if item.get("verification"):
            row["verification"] = item["verification"]
        rows.append(row)
    supplemented = {row["identifier"] for row in rows}
    gaps = [
        {
            "identifier": identifier,
            "en": row["en"],
            "reason": gap_reason(row["en"]),
        }
        for identifier, row in previous.items()
        if not row.get("zh") and identifier not in supplemented
    ]
    manifest_path = destination / "display-manifest.json"
    input_hashes = {
        "source_display": digest(source.read_bytes()),
        "supplement": digest(supplement.read_bytes()),
    }
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("state") != "completed" or manifest["inputs"] != input_hashes:
            raise ValueError("Existing output has different inputs or incomplete state")
        for name, expected in manifest["artifacts"].items():
            if digest((destination / name).read_bytes()) != expected:
                raise ValueError("Frozen output checksum mismatch")
        return manifest
    artifacts = {
        "display-translations.json": write_frozen(
            destination / "display-translations.json", rows
        ),
        "remaining-gaps.json": write_frozen(destination / "remaining-gaps.json", gaps),
    }
    manifest = {
        "schema": "biomed-additional-display-translations-v1",
        "state": "completed",
        "namespace": namespace,
        "version": version,
        "source_version": source_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": input_hashes,
        "counts": {
            "source_concepts": len(previous),
            "existing_chinese": sum(bool(row.get("zh")) for row in previous.values()),
            "added_project_draft": len(rows),
            "skipped_existing": len(skipped),
            "remaining_without_chinese": len(gaps),
        },
        "artifacts": artifacts,
    }
    write_frozen(manifest_path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--namespace", choices=["ONCOTREE", "RXNORM", "RXCLASS"], required=True
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--build-drug-dictionary", action="store_true")
    args = parser.parse_args()
    if args.namespace == "RXCLASS":
        result = build_rxclass_dictionary(
            args.source or RESOURCES / "rxclass-epc-20261025",
            args.supplement or VOCABULARIES / "rxclass-zh-seed-20261025.tsv",
            args.output,
        )
        print(json.dumps({"dictionary_entries": len(result["labels"])}))
        return
    source_defaults = {
        "ONCOTREE": RESOURCES / "cancer-20261025/display-translations.json",
        "RXNORM": RESOURCES / "drug-translations-20261025/display-translations.json",
    }
    supplement_defaults = {
        "ONCOTREE": VOCABULARIES / "oncotree_zh_supplement.json",
        "RXNORM": VOCABULARIES / "rxnorm_zh_supplement.json",
    }
    if args.build_drug_dictionary:
        if args.namespace != "RXNORM":
            parser.error("--build-drug-dictionary requires RXNORM")
        snapshot = RESOURCES / "translation-supplement-20261025"
        result = build_drug_dictionary(
            args.source or source_defaults["RXNORM"],
            VOCABULARIES / "rxnorm-zh-seed-20261025.tsv",
            snapshot / "nhc-antitumor-bilingual-headings.json",
            snapshot / "nhc-antitumor-source.json",
            args.output,
        )
        print(json.dumps({"dictionary_entries": len(result["entries"])}))
        return
    manifest = prepare(
        args.namespace,
        args.source or source_defaults[args.namespace],
        args.supplement or supplement_defaults[args.namespace],
        args.output,
    )
    print(json.dumps(manifest["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
