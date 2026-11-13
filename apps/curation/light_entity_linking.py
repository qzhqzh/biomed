"""Offline standard-term candidate retrieval, without identity confirmation.

Document aliases must already be supported by the caller's source text. A match
does not establish semantic equivalence: in particular, the frozen MONDO
snapshot does not preserve exact/broad synonym scope.
"""

import hashlib
import json
import unicodedata
from collections import defaultdict
from pathlib import Path

VERSION = "light-entity-linking-v1"
_PUNCTUATION = str.maketrans({char: "-" for char in "‐‑‒–—−"} | {"’": "'", "‘": "'"})
_KINDS = {"drug": "drug", "disease": "disease", "gene": "gene", "protein": "gene"}


def _key(value):
    # Preserve +, /, parentheses and all words: they may identify combinations,
    # stereochemistry or qualifiers. No substring/fuzzy/suffix deletion.
    return " ".join(
        unicodedata.normalize("NFC", value).translate(_PUNCTUATION).casefold().split()
    )


def _identity(candidate):
    return (
        candidate["namespace"],
        candidate["identifier"],
        json.dumps(candidate["version"], sort_keys=True),
    )


def _resource(value, collection, default_version):
    if isinstance(value, (str, Path)):
        raw = Path(value).read_bytes()
        value = json.loads(raw)
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    if isinstance(value, list):
        return value, default_version, sha256
    if isinstance(value, dict):
        rows = value.get(collection, value.get("records"))
        if isinstance(rows, list):
            return rows, value.get("version", default_version), sha256
    raise ValueError(f"Expected a {collection} array or versioned resource object")


def load_index(snapshot, oncotree=None, chembl=None):
    """Build a read-only index from an existing snapshot object or JSON path."""
    if isinstance(snapshot, (str, Path)):
        snapshot = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    index = defaultdict(list)

    def add(kind, record, names):
        for field, name in names:
            if isinstance(name, str) and name.strip():
                index[(kind, _key(name))].append((record, field, name))

    for concept in snapshot["concepts"]:
        release = snapshot["releases"][concept["release_id"]]
        namespace = release["namespace"].upper()
        kind = {"RXNORM": "drug", "MONDO": "disease"}.get(namespace)
        if kind is None:
            continue
        identifier = str(concept["identifier"])
        record = {
            "id": identifier if ":" in identifier else f"{namespace}:{identifier}",
            "namespace": namespace,
            "identifier": identifier,
            "label": concept["label"],
            "version": release["version"],
            "source_sha256": release.get("sha256"),
            "source_url": release.get("source_url"),
            "entity_level": "drug_ingredient" if kind == "drug" else "disease_concept",
            "synonym_scope": "not_preserved" if kind == "disease" else "source_alias",
        }
        add(
            kind,
            record,
            [
                ("label", concept["label"]),
                *[("synonym", name) for name in concept.get("synonyms", [])],
            ],
        )

    for gene in snapshot["genes"]:
        if not gene.get("gene_id"):
            continue
        record = {
            "id": f"NCBIGene:{gene['gene_id']}",
            "namespace": "NCBIGene",
            "identifier": str(gene["gene_id"]),
            "label": gene["symbol"],
            "version": snapshot["gene_version"],
            "source_sha256": snapshot["gene_version"].get("genes_sha256"),
            "entity_level": "gene",
            "synonym_scope": "source_alias",
        }
        add(
            "gene",
            record,
            [
                ("label", gene["symbol"]),
                ("name", gene.get("name")),
                *[("synonym", name) for name in gene.get("synonyms", [])],
            ],
        )
    if oncotree is not None:
        nodes, version, source_sha = _resource(oncotree, "nodes", "oncotree_2025_10_03")
        for node in nodes:
            identifier = node.get("identifier") or f"ONCOTREE:{node['code']}"
            record = {
                "id": identifier,
                "namespace": "ONCOTREE",
                "identifier": identifier,
                "label": node["en"],
                "version": version,
                "source_sha256": source_sha,
                "entity_level": "cancer_classification_node",
                "synonym_scope": "source_code",
                "level": node.get("level"),
                "parent": node.get("parent"),
            }
            add("disease", record, [("label", node["en"]), ("code", node.get("code"))])

    if chembl is not None:
        molecules, version, source_sha = _resource(chembl, "molecules", "37")
        for molecule in molecules:
            identifier = molecule["molecule_chembl_id"]
            record = {
                "id": f"CHEMBL:{identifier}",
                "namespace": "CHEMBL",
                "identifier": identifier,
                "label": molecule.get("pref_name") or identifier,
                "version": version,
                "source_sha256": source_sha,
                "source_record_sha256": molecule.get("_source", {}).get("sha256"),
                "entity_level": "molecule",
                "synonym_scope": "source_alias",
                "molecule_type": molecule.get("molecule_type"),
                "parent_chembl_id": (molecule.get("molecule_hierarchy") or {}).get(
                    "parent_chembl_id"
                ),
            }
            names = [("label", molecule.get("pref_name")), ("identifier", identifier)]
            for synonym in molecule.get("molecule_synonyms", []):
                names.extend(
                    ("synonym", synonym.get(field))
                    for field in ("molecule_synonym", "synonyms")
                )
            add("drug", record, names)
    return dict(index)


def retrieve(index, kind, mention, aliases=(), limit=5):
    """Return ranked whole-name matches; never select an ID or infer a new one.

    The same external ID/version is one candidate even when multiple source
    aliases match. `total_candidates` counts all identities before top-k; a
    truncated ambiguous result must not be treated as unique.
    """
    if not isinstance(mention, str) or not mention.strip():
        raise ValueError("mention must be a nonempty string")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20")
    if isinstance(aliases, str) or not isinstance(aliases, (list, tuple)):
        raise TypeError("aliases must be a sequence of source-supported strings")
    if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
        raise ValueError("aliases must contain nonempty strings")
    result = {
        "version": VERSION,
        "kind": kind,
        "mention": mention,
        "status": "unresolved",
        "confirmed_id": None,
        "candidates": [],
        "total_candidates": 0,
        "truncated": False,
        "note": "Candidate retrieval only; source location, type, context and identity still require validation.",
    }
    if kind not in _KINDS:
        result["status"] = "unsupported_type"
        return result
    matches = {}
    for query_index, query in enumerate(dict.fromkeys([mention, *aliases])):
        for record, field, source_name in index.get((_KINDS[kind], _key(query)), []):
            literal = query == source_name
            rank = (query_index > 0, field != "label", not literal)
            evidence = {
                "query": query,
                "query_source": "mention" if query_index == 0 else "document_alias",
                "dictionary_text": source_name,
                "dictionary_field": field,
                "match_method": "literal" if literal else "normalized_whole_name",
            }
            identity = _identity(record)
            candidate = matches.setdefault(
                identity, {**record, "matching_evidence": [], "_rank": rank}
            )
            candidate["_rank"] = min(candidate["_rank"], rank)
            if evidence not in candidate["matching_evidence"]:
                candidate["matching_evidence"].append(evidence)
    candidates = sorted(matches.values(), key=lambda item: (item["_rank"], item["id"]))
    result["total_candidates"] = len(candidates)
    result["truncated"] = len(candidates) > limit
    result["status"] = (
        "ambiguous"
        if len(candidates) > 1
        else "unique_candidate"
        if candidates
        else "unresolved"
    )
    for candidate in candidates[:limit]:
        candidate.pop("_rank")
        candidate["requires_identity_review"] = True
        candidate["link_level"] = (
            "associated_gene_not_protein_isoform"
            if kind == "protein"
            else candidate["entity_level"]
        )
        result["candidates"].append(candidate)
    return result
