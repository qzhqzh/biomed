"""Exact source-identity display labels, independent of models and HTTP views."""

import json
from functools import lru_cache
from pathlib import Path

TRANSLATION_FILE = Path(__file__).with_name("vocabularies") / "rxclass-zh-20261025.json"


def class_translations(identifier, label):
    if not TRANSLATION_FILE.exists():
        return []
    vocabulary = _translation_vocabulary(
        str(TRANSLATION_FILE), TRANSLATION_FILE.stat().st_mtime_ns
    )
    entry = vocabulary.get("labels", {}).get(identifier)
    if not entry or entry.get("en") != label or not entry.get("zh", "").strip():
        return []
    source = vocabulary.get("source", "")
    return [
        {
            "label": entry["zh"],
            "language": "zh-Hans",
            "provider": "project_rxclass_translation",
            "status": "project_draft",
            "source_url": source
            if isinstance(source, str) and source.startswith("https://")
            else "",
            "source_version": vocabulary.get("version", ""),
            "match_basis": entry.get(
                "match_basis", vocabulary.get("method", "project translation")
            ),
        }
    ]


@lru_cache(maxsize=2)
def _translation_vocabulary(path, modified):
    return json.loads(Path(path).read_text())
