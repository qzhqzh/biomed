"""Emit a read-only snapshot of current biomed dictionaries for offline linking.

Run inside the existing web container; stdout contains public standard concepts,
not patient or account records. This does not access tumor_api.
"""

import hashlib
import json
import os
import sqlite3

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()
from django.conf import settings

from apps.annotations.knowledge import store

path = settings.DATABASES["default"]["NAME"]
db = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3)
try:
    db.execute("BEGIN")
    releases = {
        row[0]: dict(
            zip(["id", "namespace", "version", "source_url", "rights", "sha256"], row)
        )
        for row in db.execute(
            "SELECT id,namespace,version,source_url,rights,sha256 FROM curation_sourcerelease WHERE active=1"
        )
    }
    concepts = []
    for release_id in releases:
        for identifier, label, synonyms in db.execute(
            "SELECT identifier,label,synonyms FROM curation_concept WHERE release_id=?",
            (release_id,),
        ):
            concepts.append(
                {
                    "release_id": release_id,
                    "identifier": identifier,
                    "label": label,
                    "synonyms": json.loads(synonyms),
                }
            )
finally:
    db.close()
with store.pin_snapshot():
    genes = store.ensure_loaded().gene_rows
    version = store.data_version()
    gene_path = store.ensure_loaded().path / "genes.jsonl"
    version["genes_sha256"] = hashlib.sha256(gene_path.read_bytes()).hexdigest()
    snapshot = {
        "releases": releases,
        "concepts": concepts,
        "gene_version": version,
        "genes": genes,
    }
print(json.dumps(snapshot, ensure_ascii=False))
