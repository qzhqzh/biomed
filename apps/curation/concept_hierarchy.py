"""Read a single immutable release as a directed, possibly multi-parent graph."""

from functools import lru_cache

from apps.curation.models import Concept


@lru_cache(maxsize=4)
def release_graph(release_id):
    nodes = {
        row["identifier"]: row
        for row in Concept.objects.filter(release_id=release_id).values(
            "identifier", "label", "parents"
        )
    }
    children = {identifier: set() for identifier in nodes}
    entries = set()
    for identifier, node in nodes.items():
        # Some valid MONDO entry nodes inherit an external ontology's class.
        # Expose them as local entry points, retaining their actual parents.
        if not any(parent in nodes for parent in node["parents"]):
            entries.add(identifier)
        for parent in node["parents"]:
            if parent in children:
                children[parent].add(identifier)
    return nodes, children, entries


def branch(release_id, parent):
    nodes, children, roots = release_graph(str(release_id))
    if parent == "__roots__":
        return roots, {"focus": None, "parents": [], "missing_parents": []}
    if parent not in nodes:
        raise ValueError("Classification node does not exist in this release")
    node = nodes[parent]
    return children[parent], {
        "focus": {"identifier": parent, "label": node["label"]},
        "parents": [
            {"identifier": p, "label": nodes[p]["label"]}
            for p in node["parents"]
            if p in nodes
        ],
        "missing_parents": [p for p in node["parents"] if p not in nodes],
    }


def tree_path(release_id, identifier):
    """Return a complete single-parent path; never invent a root or flatten a DAG."""
    nodes, _, _ = release_graph(str(release_id))
    path = []
    seen = set()
    while True:
        if identifier not in nodes:
            raise ValueError("Classification node does not exist in this release")
        if identifier in seen:
            raise ValueError("Classification path contains a cycle")
        seen.add(identifier)
        path.append(identifier)
        parents = nodes[identifier]["parents"]
        if not parents:
            return list(reversed(path))
        if len(parents) != 1:
            raise ValueError("Classification path is not a single-parent tree")
        identifier = parents[0]
