"""Optional PubTator reference lane; never replaces local extraction results.

Only public PMIDs from selected extraction packets are requested. The cache is
separate from the production DB; a process-wide file lock serializes requests
across batches, and immutable response objects preserve earlier provenance.
"""

import bisect
import difflib
import fcntl
import hashlib
import json
import os
import re
import time
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "pubtator-reference-v1"
CACHE_ROOT = Path("/appdata/02_resource/apps/biomed/resources/pubtator3")
ENDPOINT = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
INTERVAL = 2.0
RATE = 256 * 1024
MAX_BYTES = 16 * 1024 * 1024
BATCH_SIZE = 10
RETRY_DELAYS = (5, 15)
FAILURE_COOLDOWN = 30 * 60
POSITIVE_TTL = 7 * 86400
PARTIAL_TTL = 86400


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class PubTatorError(Exception):
    pass


class TransientError(PubTatorError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PubTatorError("PubTator redirected; endpoint needs review")


def retry_after(value, now):
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return FAILURE_COOLDOWN
    if not 0 <= delay <= 7 * 86400:
        return 7 * 86400
    return delay


def download(url, *, sleep=time.sleep, monotonic=time.monotonic):
    # The caller constructs this endpoint; redirects cannot escape it.
    if not url.startswith(ENDPOINT + "?"):
        raise PubTatorError("Unexpected PubTator endpoint")
    request = Request(url, headers={"User-Agent": "biomed-pubtator/1.0",
                                    "Accept": "application/json", "Accept-Encoding": "identity"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            if response.headers.get("Content-Encoding", "identity") not in ("", "identity"):
                raise PubTatorError("Unexpected encoded PubTator response")
            start, size, chunks = monotonic(), 0, []
            while size <= MAX_BYTES:
                if monotonic() - start > 90:
                    raise TransientError("PubTator response exceeded time budget")
                chunk = response.read(min(32 * 1024, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                sleep(max(0, size / RATE - (monotonic() - start)))
            if size > MAX_BYTES:
                raise PubTatorError("PubTator response exceeds 16 MiB")
            return b"".join(chunks)
    except HTTPError as exc:
        if exc.code in {408, 429, 500, 502, 503, 504}:
            raise TransientError(f"HTTP {exc.code}", retry_after(exc.headers.get("Retry-After"), time.time())) from exc
        raise PubTatorError(f"HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, IncompleteRead, RemoteDisconnected) as exc:
        raise TransientError(type(exc).__name__) from exc


def parse_documents(raw, requested):
    if len(raw) > MAX_BYTES:
        raise PubTatorError("Response exceeds size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PubTatorError("Invalid PubTator JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("PubTator3"), list):
        raise PubTatorError("Unexpected PubTator schema")
    documents = {}
    for doc in value["PubTator3"]:
        if not isinstance(doc, dict):
            raise PubTatorError("Invalid document")
        pmid = str(doc.get("pmid", ""))
        if pmid not in requested or pmid in documents or not isinstance(doc.get("passages"), list):
            raise PubTatorError("Response PMID mismatch/duplicate or missing passages")
        for passage in doc["passages"]:
            if (not isinstance(passage, dict) or not isinstance(passage.get("text"), str)
                    or type(passage.get("offset")) is not int or not isinstance(passage.get("annotations", []), list)):
                raise PubTatorError("Invalid BioC passage")
            if not isinstance(passage.get("infons", {}), dict):
                raise PubTatorError("Invalid BioC section metadata")
        documents[pmid] = doc
    return documents


def has_body(doc):
    return bool(doc and any(
        str(p.get("infons", {}).get("section_type", p.get("infons", {}).get("type", ""))).lower()
        not in {"", "title", "abstract"} for p in doc["passages"]
    ))


class Cache:
    def __init__(self, root=CACHE_ROOT, *, fetcher=download, sleep=time.sleep, clock=time.time):
        self.root, self.fetcher, self.sleep, self.clock = Path(root), fetcher, sleep, clock
        self.requests = 0
        self.response_bytes = 0

    def _cached(self, pmid, mode):
        path = self.root / "index" / f"{pmid}-{mode}.json"
        if not path.exists():
            return None
        entry = read(path)
        if entry.get("pmid") != pmid or entry.get("mode") != mode or entry.get("version") != VERSION:
            raise PubTatorError("Cache entry identity mismatch")
        if entry["expires_at"] <= self.clock():
            return None
        digest = entry["response_sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PubTatorError("Invalid cache response identifier")
        raw = (self.root / "objects" / f"{digest}.json").read_bytes()
        if sha(raw) != digest:
            raise PubTatorError("Cached PubTator response checksum mismatch")
        documents = parse_documents(raw, set(entry["requested_pmids"]))
        return {**entry, "cache_hit": True, "document": documents.get(pmid)}

    def _store(self, pmids, mode, raw, url):
        documents = parse_documents(raw, set(pmids))
        digest, now = sha(raw), self.clock()
        path = self.root / "objects" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if sha(path.read_bytes()) != digest:
                raise PubTatorError("Existing immutable response is corrupt")
        else:
            temp = path.with_suffix(".tmp")
            try:
                with temp.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        result = {}
        for pmid in pmids:
            doc = documents.get(pmid)
            state = "missing" if doc is None else "fulltext" if has_body(doc) else "abstract_only"
            ttl = PARTIAL_TTL if state == "missing" or (mode == "full" and state != "fulltext") else POSITIVE_TTL
            entry = {"version": VERSION, "pmid": pmid, "mode": mode, "state": state,
                     "url": url, "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                     "expires_at": now + ttl, "response_sha256": digest,
                     "response_file": str(path.resolve()), "response_bytes": len(raw), "requested_pmids": pmids}
            atomic_json(self.root / "index" / f"{pmid}-{mode}.json", entry)
            result[pmid] = {**entry, "cache_hit": False, "document": doc}
        return result

    def get_many(self, pmids, mode):
        if mode not in {"abstract", "full"}:
            raise ValueError("Unsupported PubTator mode")
        pmids = sorted({str(p) for p in pmids})
        if any(not re.fullmatch(r"[1-9]\d{0,9}", p) for p in pmids):
            raise ValueError("Only validated PMIDs are accepted")
        if not pmids:
            return {}
        self.root.mkdir(parents=True, exist_ok=True)
        result = {}
        with (self.root / ".fetch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {p: {"state": "unavailable", "reason": "cache_busy"} for p in pmids}
            pending = []
            for pmid in pmids:
                try:
                    entry = self._cached(pmid, mode)
                except (OSError, ValueError, KeyError, PubTatorError) as exc:
                    result[pmid] = {"state": "unavailable", "reason": "cache_invalid", "error": str(exc)}
                    continue
                if entry is None:
                    pending.append(pmid)
                else:
                    result[pmid] = entry
            throttle_path = self.root / "throttle.json"
            throttle = read(throttle_path) if throttle_path.exists() else {}
            for offset in range(0, len(pending), BATCH_SIZE):
                group = pending[offset:offset + BATCH_SIZE]
                if throttle.get("retry_at", 0) > self.clock():
                    result.update({p: {"state": "unavailable", "reason": "source_cooldown",
                                       "retry_at": throttle["retry_at"]} for p in group})
                    continue
                params = {"pmids": ",".join(group)}
                if mode == "full":
                    params["full"] = "true"
                url = ENDPOINT + "?" + urlencode(params)
                for attempt in range(len(RETRY_DELAYS) + 1):
                    self.sleep(max(0, INTERVAL - (self.clock() - throttle.get("last_request", 0))))
                    throttle["last_request"] = self.clock()
                    atomic_json(throttle_path, throttle)
                    self.requests += 1
                    try:
                        raw = self.fetcher(url)
                        self.response_bytes += len(raw)
                        result.update(self._store(group, mode, raw, url))
                        throttle.pop("retry_at", None)
                        throttle.pop("last_failure", None)
                        atomic_json(throttle_path, throttle)
                        break
                    except (PubTatorError, OSError) as exc:
                        delay = max(RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else FAILURE_COOLDOWN,
                                    getattr(exc, "retry_after", 0))
                        throttle.update(last_failure={"at": self.clock(), "url": url,
                                                       "attempt": attempt + 1, "error": str(exc)},
                                        retry_at=self.clock() + delay)
                        atomic_json(throttle_path, throttle)
                        if not isinstance(exc, TransientError) or attempt == len(RETRY_DELAYS) or delay > 15:
                            throttle["retry_at"] = self.clock() + max(delay, FAILURE_COOLDOWN)
                            atomic_json(throttle_path, throttle)
                            result.update({p: {"state": "unavailable", "reason": "request_failed",
                                               "retry_at": throttle["retry_at"], "error": str(exc)} for p in group})
                            break
                        self.sleep(delay)
            return result


def folded(text):
    letters, positions = [], []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            if normalized.isalnum():
                letters.append(normalized)
                positions.append(index)
    return "".join(letters), positions


def project_passage(source, document):
    """Bind a remote annotation to an exact quote in this local source version."""
    left, source_positions = folded(source["text"])
    candidates = []
    for index, passage in enumerate(document["passages"]):
        if str(passage.get("infons", {}).get("section_type", "")).upper() == "REF":
            continue
        right, positions = folded(passage["text"])
        matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
        if not left or matcher.quick_ratio() < 0.98:
            continue
        ratio = matcher.ratio()
        if ratio >= (1.0 if len(left) < 40 else 0.98):
            candidates.append((ratio, index, matcher, positions))
    if not candidates:
        return {"state": "no_matching_text", "annotations": []}
    candidates.sort(key=lambda item: (-item[0], item[1]))
    ratio, index, matcher, positions = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == ratio:
        other = document["passages"][candidates[1][1]]["text"]
        if folded(other)[0] != folded(document["passages"][index]["text"])[0]:
            return {"state": "ambiguous_text", "annotations": []}
    passage = document["passages"][index]
    mapping = {block.b + i: block.a + i for block in matcher.get_matching_blocks() for i in range(block.size)}
    annotations, rejected = [], 0
    for annotation in passage.get("annotations", []):
        if (not isinstance(annotation, dict) or not isinstance(annotation.get("text"), str)
                or not isinstance(annotation.get("infons", {}), dict)
                or not isinstance(annotation.get("locations", []), list)):
            rejected += 1
            continue
        for location in annotation.get("locations", []):
            if (not isinstance(location, dict) or type(location.get("offset")) is not int or type(location.get("length")) is not int
                    or location["length"] <= 0):
                rejected += 1
                continue
            start = location["offset"] - passage["offset"]
            end = start + location["length"]
            if start < 0 or passage["text"][start:end] != annotation["text"]:
                rejected += 1
                continue
            indices = range(bisect.bisect_left(positions, start), bisect.bisect_left(positions, end))
            if not indices or any(i not in mapping for i in indices):
                rejected += 1
                continue
            mapped = [mapping[i] for i in indices]
            if mapped != list(range(mapped[0], mapped[-1] + 1)):
                rejected += 1
                continue
            local_start, local_end = source_positions[mapped[0]], source_positions[mapped[-1]] + 1
            quote = source["text"][local_start:local_end]
            if folded(quote)[0] != folded(annotation["text"])[0]:
                rejected += 1
                continue
            annotations.append({"annotation_id": annotation.get("id"), "text": quote,
                                "remote_text": annotation["text"], "infons": annotation.get("infons", {}),
                                "location": {"passage_id": source["id"], "role": source["role"],
                                             "start": local_start, "end": local_end},
                                "remote_location": location, "remote_passage_index": index})
    return {"state": "aligned", "similarity": ratio, "annotations": annotations, "rejected_offsets": rejected}


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def dictionary_indexes(standards):
    genes, drugs = {}, {}
    for gene in standards["genes"]:
        if not gene.get("gene_id"):
            continue
        for alias in [gene["symbol"], gene.get("name", ""), *gene.get("synonyms", [])]:
            genes.setdefault(name_key(alias), set()).add(str(gene["gene_id"]))
    for concept in standards["concepts"]:
        if standards["releases"][concept["release_id"]]["namespace"].upper() == "RXNORM":
            drugs.setdefault(name_key(concept["label"]), set()).add(concept["identifier"])
    return genes, drugs


def suggest(packet, annotations, indexes):
    """Return separate candidates; do not mutate local inventory or relations."""
    genes, drugs = indexes
    accepted, rejected, seen = [], [], set()
    for annotation in annotations:
        info, name = annotation["infons"], name_key(annotation["text"])
        kind, identifier = info.get("type"), str(info.get("identifier", ""))
        target_type, target_id = None, None
        if kind == "Gene" and identifier.isdecimal() and genes.get(name) == {identifier}:
            target_type, target_id = "gene", f"NCBIGene:{identifier}"
        elif kind == "Chemical" and identifier.startswith("MESH:"):
            same = [e for e in packet.get("entities", []) if name_key(e["text"]) == name and e["type"] in {"drug", "treatment"}]
            local = drugs.get(name_key(info.get("name", "")), set())
            if same and len(local) == 1:
                target_type, target_id = "drug", next(iter(local))
        if target_type is None:
            rejected.append({"annotation_id": annotation["annotation_id"], "text": annotation["text"],
                             "reason": "outside_policy_or_dictionary_disagreement"})
            continue
        identity = (target_type, name, target_id)
        if identity in seen:
            continue
        seen.add(identity)
        same_typed = [e for e in packet.get("entities", []) if name_key(e["text"]) == name
                      and e["type"] in ({"gene", "protein"} if target_type == "gene" else {"drug"})]
        existing_ids = {e.get("link", {}).get("candidate_id") for e in same_typed} - {None}
        if existing_ids and existing_ids != {target_id}:
            rejected.append({"text": annotation["text"], "reason": "existing_local_identity_conflict"})
            continue
        if existing_ids == {target_id}:
            continue
        accepted.append({"id": f"{packet['id']}.pt-{sha(repr(identity).encode())[:12]}",
                         "type": target_type, "text": annotation["text"],
                         "action": "link_candidate" if same_typed else "entity_candidate",
                         "existing_entity_ids": [e["id"] for e in same_typed],
                         "locations": [annotation["location"]], "roles": ["context"],
                         "link": {"candidate_id": target_id, "status": "dictionary_supported_candidate"},
                         "source": annotation["source"], "remote_annotation_id": annotation["annotation_id"],
                         "relation_inferred": False})
    return accepted, rejected


def build_reference(dataset, cache, standards):
    """The caller must supply only the selected, actually processed inventory."""
    packets = dataset.get("records", [])
    pmids = sorted({str(p["article"].get("pmid", "")) for p in packets
                    if re.fullmatch(r"[1-9]\d{0,9}", str(p["article"].get("pmid", "")))})
    fetched = {mode: cache.get_many(pmids, mode) for mode in ("full", "abstract")}
    indexes, projected, rows = dictionary_indexes(standards), {}, []
    for packet in packets:
        pmid = str(packet["article"].get("pmid", ""))
        annotations, alignment = [], []
        for passage in packet["passages"]:
            best = None
            for mode in ("full", "abstract"):
                entry = fetched[mode].get(pmid, {})
                if not entry.get("document"):
                    continue
                key = (entry["response_sha256"], pmid, passage["id"], passage["role"], sha(passage["text"].encode()))
                if key not in projected:
                    projected[key] = project_passage(passage, entry["document"])
                trial = projected[key]
                if trial["state"] == "aligned":
                    best = {**trial, "mode": mode}
                    source = {k: entry[k] for k in ("pmid", "mode", "url", "fetched_at", "response_sha256", "response_file")}
                    annotations.extend({**a, "source": source} for a in trial["annotations"])
                    break
            alignment.append({"passage_id": passage["id"], **{k: v for k, v in (best or {"state": "no_matching_text"}).items() if k != "annotations"}})
        if packet.get("entity_stage", {}).get("model_status") == "classified":
            candidates, rejected = suggest(packet, annotations, indexes)
        else:
            candidates, rejected = [], [{"reason": "local_model_not_successful_no_replacement"}]
        rows.append({"id": packet["id"], "article": deepcopy(packet["article"]), "alignment": alignment,
                     "annotations": annotations, "candidates": candidates, "rejected": rejected,
                     "local_entities_unchanged": True})
    articles = {p: {mode: {k: v for k, v in fetched[mode].get(p, {}).items() if k != "document"}
                    for mode in fetched} for p in pmids}
    summary = {"version": VERSION, "mode": "dual_reference", "local_models_required": ["9B", "Bonsai"],
               "local_models_skipped": False, "local_result_overwritten": False,
               "articles": len(pmids), "groups": len(rows), "requests": cache.requests,
               "response_bytes": cache.response_bytes,
               "candidates": sum(len(r["candidates"]) for r in rows),
               "fulltext_articles": sum(articles[p]["full"].get("state") == "fulltext" for p in pmids),
               "cache_hits": sum(bool(v.get("cache_hit")) for a in articles.values() for v in a.values()),
               "missing_views": sum(v.get("state") == "missing" for a in articles.values() for v in a.values()),
               "unavailable_views": sum(v.get("state") == "unavailable" for a in articles.values() for v in a.values()),
               "limits": {"interval_seconds": INTERVAL, "bytes_per_second": RATE, "concurrency": 1},
               "relations_imported": False, "target_written": False}
    return {"summary": summary, "articles": articles, "records": rows}
