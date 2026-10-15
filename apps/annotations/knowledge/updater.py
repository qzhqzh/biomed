"""Check fixed NCBI sources and atomically publish validated local snapshots."""

from __future__ import annotations

import csv
import fcntl
import gzip
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from apps.annotations.knowledge.releases import active_dir, updates_dir
from scripts.import_gene_knowledge import (
    GENE_INFO_URL, MANE_CURRENT, MANE_README_URL,
    parse_gene_info, parse_mane_readme, parse_mane_summary,
)

logger = logging.getLogger(__name__)
RAW_NAMES = {"gene_info": "Homo_sapiens.gene_info.gz", "mane": "MANE.summary.txt.gz"}
MIN_ROWS = 10_000
# NCBI's Homo_sapiens file also contains Neanderthal and Denisovan records.
HUMAN_TAXA = {"9606", "63221", "741158"}


class UpdateBusy(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_json(path, value):
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def acquire_lock():
    updates_dir().mkdir(parents=True, exist_ok=True)
    handle = (updates_dir() / "update.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise UpdateBusy("已有检查或更新正在运行") from None
    return handle


def get_status():
    state = read_json(updates_dir() / "status.json")
    if state.get("status") == "running":
        try:
            lock = acquire_lock()
        except UpdateBusy:
            pass
        else:
            # Re-read under the lock: another worker may have just completed.
            try:
                state = read_json(updates_dir() / "status.json")
                if state.get("status") == "running":
                    state.update(status="interrupted", message="任务已中断，可重新检查；已发布数据不受影响。")
                    write_json(updates_dir() / "status.json", state)
            finally:
                lock.close()
    meta = read_json(active_dir() / "meta.json")
    return {
        "status": "idle", "message": "尚未检查上游数据", **state,
        "current": meta,
        "sources_config": {"gene_info": GENE_INFO_URL, "mane": MANE_CURRENT},
        "schedule": "manual",
    }


def content_hash(path):
    digest = hashlib.sha256()
    total = 0
    with gzip.open(path, "rb") as source:
        while chunk := source.read(256 * 1024):
            total += len(chunk)
            if total > 512 * 1024 * 1024:
                raise ValueError("解压文件超过安全大小限制")
            digest.update(chunk)
    return digest.hexdigest()


def check_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "ftp.ncbi.nlm.nih.gov":
        raise ValueError("更新仅允许访问固定 NCBI HTTPS 数据源")


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, max_bytes=128 * 1024 * 1024):
    check_url(url)
    opener = urllib.request.build_opener(OfficialRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "online-biotools/knowledge-updater"})
    digest = hashlib.sha256()
    started = time.monotonic()
    written = 0
    with opener.open(request, timeout=45) as response, destination.open("wb") as output:
        if response.status != 200:
            raise ValueError(f"数据源响应异常：HTTP {response.status}")
        expected = response.headers.get("Content-Length")
        for chunk in iter(lambda: response.read(256 * 1024), b""):
            written += len(chunk)
            if written > max_bytes or time.monotonic() - started > 600:
                raise ValueError("下载超过大小或时间限制")
            output.write(chunk)
            digest.update(chunk)
        if expected and written != int(expected):
            raise ValueError("下载不完整，已拒绝更新")
        return {"url": url, "last_modified": response.headers.get("Last-Modified"),
                "etag": response.headers.get("ETag"), "bytes": written,
                "sha256": digest.hexdigest()}


def validate_rows(path, required, kind):
    with gzip.open(path, "rt", encoding="utf-8") as source:
        header = source.readline().rstrip("\r\n").lstrip("#").split("\t")
        if not required.issubset(header):
            raise ValueError(f"{kind} 文件缺少必要字段")
        seen = set()
        for columns in csv.reader(source, delimiter="\t"):
            if len(columns) != len(header):
                raise ValueError(f"{kind} 存在不完整的数据行")
            row = dict(zip(header, columns))
            if kind == "Gene":
                key = row["GeneID"]
                if row["tax_id"] not in HUMAN_TAXA or not key.isdigit() or int(key) <= 0:
                    raise ValueError("Gene 物种或 GeneID 无效")
            else:
                key = row["RefSeq_nuc"]
                if (row["MANE_status"] not in {"MANE Select", "MANE Plus Clinical"}
                        or not re.fullmatch(r"N[MR]_\d+\.\d+", key)
                        or not re.fullmatch(r"ENST\d+\.\d+", row["Ensembl_nuc"])
                        or not re.fullmatch(r"GeneID:\d+", row["NCBI_GeneID"])):
                    raise ValueError("MANE 标识符或集合类型无效")
                for field, pattern in (("RefSeq_prot", r"NP_\d+\.\d+"),
                                       ("Ensembl_prot", r"ENSP\d+\.\d+")):
                    if row[field] not in {"", "-"} and not re.fullmatch(pattern, row[field]):
                        raise ValueError("MANE 蛋白标识符无效")
            if key in seen:
                raise ValueError(f"{kind} 出现重复主键")
            seen.add(key)
        if len(seen) < MIN_ROWS:
            raise ValueError(f"{kind} 数据量异常：{len(seen)} 条")


def build_candidate(directory, previous):
    raw = directory / "raw"
    validate_rows(raw / RAW_NAMES["gene_info"],
                  {"tax_id", "GeneID", "Symbol", "description", "type_of_gene", "dbXrefs", "Synonyms"}, "Gene")
    validate_rows(raw / RAW_NAMES["mane"],
                  {"NCBI_GeneID", "symbol", "RefSeq_nuc", "Ensembl_nuc", "RefSeq_prot", "Ensembl_prot", "MANE_status"}, "MANE")
    genes = parse_gene_info(raw / RAW_NAMES["gene_info"], directory / "genes.jsonl")
    records, transcripts = parse_mane_summary(raw / RAW_NAMES["mane"])
    for current, old, label in (
        (genes["gene_count"], previous.get("genes", {}).get("gene_count", 0), "Gene"),
        (transcripts["mane_select"], previous.get("mane", {}).get("mane_select", 0), "MANE Select"),
    ):
        if old and current < old * 0.8:
            raise ValueError(f"{label} 数量下降超过 20%，需人工核对，未发布")
    write_json(directory / "transcript-map.json", {"records": records})
    return genes, transcripts


def check_and_publish(action, report):
    previous_dir = active_dir()
    previous = read_json(previous_dir / "meta.json")
    previous_sources = previous.get("updates", {}).get("sources", {})
    with tempfile.TemporaryDirectory(prefix="staging-", dir=updates_dir()) as temporary:
        directory = Path(temporary)
        raw = directory / "raw"
        raw.mkdir()
        report("读取 MANE 官方版本信息")
        download(MANE_README_URL, raw / "README_versions.txt", 1024 * 1024)
        release = parse_mane_readme((raw / "README_versions.txt").read_text(encoding="utf-8"))
        version = release.get("MANE Version", "")
        if not re.fullmatch(r"\d+(?:\.\d+)+", version):
            raise ValueError("无法识别 MANE 官方版本号")
        sources = {}
        for key, url in (("gene_info", GENE_INFO_URL),
                         ("mane", f"{MANE_CURRENT}MANE.GRCh38.v{version}.summary.txt.gz")):
            report(f"下载并核验 {'Gene' if key == 'gene_info' else 'MANE'} 数据内容")
            path = raw / RAW_NAMES[key]
            source = download(url, path)
            source["content_sha256"] = content_hash(path)
            old_hash = previous_sources.get(key, {}).get("content_sha256")
            old_raw = previous_dir / "raw" / RAW_NAMES[key]
            if not old_hash and old_raw.is_file():
                try:
                    old_hash = content_hash(old_raw)
                except (OSError, EOFError, ValueError):
                    pass  # A damaged/missing baseline must be rebuilt, not reported current.
            source["changed"] = source["content_sha256"] != old_hash
            if key == "mane":
                source["version"] = version
                source["changed"] |= version != previous.get("mane", {}).get("version")
            source["last_updated_at"] = previous_sources.get(key, {}).get("last_updated_at", previous.get("imported_at"))
            sources[key] = source
        # The moving 'current' directory must not cross releases during a check.
        download(MANE_README_URL, directory / "readme-confirm.txt", 1024 * 1024)
        if parse_mane_readme((directory / "readme-confirm.txt").read_text(encoding="utf-8")) != release:
            raise ValueError("MANE 正在切换版本，请稍后重新检查")
        report("校验字段、物种、主键和数据数量")
        genes, transcripts = build_candidate(directory, previous)
        files_ready = all((previous_dir / name).is_file() for name in ("genes.jsonl", "transcript-map.json", "meta.json"))
        changed = any(source["changed"] for source in sources.values()) or not files_ready
        checked_at = now()
        result = {"sources": sources, "last_checked_at": checked_at, "update_available": changed,
                  "candidate_counts": {"genes": genes["gene_count"], **transcripts}, "published": False}
        if not changed:
            return {**result, "message": "内容一致，无需更新；本地更新时间保持不变。"}
        if action == "check":
            return {**result, "message": "发现变化或本地数据缺失，可执行「检查并更新」。"}
        report("发布已校验的数据快照")
        release_id = uuid.uuid4().hex
        for source in sources.values():
            if source["changed"]:
                source["last_updated_at"] = checked_at
        mane = {
            **previous.get("mane", {}), "version": version, "assembly": "GRCh38",
            "assembly_note": "MANE 基于 GRCh38 定义；其他组装的官方映射需另行下载并核对序列差异。",
            "refseq_annotation": release.get("NCBI RefSeq Annotation Release"),
            "ensembl_release": release.get("Ensembl Release"),
            "ftp_current": MANE_CURRENT, "summary_url": sources["mane"]["url"], **transcripts,
        }
        meta = {**previous, "release": release_id, "imported_at": checked_at,
                "genes": genes, "transcripts": transcripts, "mane": mane,
                "sources": {key: {"url": item["url"], "file": "raw/" + RAW_NAMES[key]} for key, item in sources.items()},
                "updates": {"sources": sources, "previous_release": previous.get("release", "initial")}}
        write_json(directory / "meta.json", meta)
        destination = updates_dir() / "releases" / release_id
        destination.parent.mkdir(exist_ok=True)
        # Staging is private (0700); published public reference data is readable
        # by host-side tools as well as the container's service account.
        directory.chmod(0o755)
        os.replace(directory, destination)
        write_json(updates_dir() / "current.json", {"release": release_id})
        return {**result, "update_available": False, "published": True,
                "message": "更新成功，新查询已切换到新快照；旧快照已保留。"}


def start_update(action, *, background=True):
    if not isinstance(action, str) or action not in {"check", "update"}:
        raise ValueError("action 只能是 check 或 update")
    lock = acquire_lock()
    state_path = updates_dir() / "status.json"
    try:
        old = read_json(state_path)
        state = {"status": "running", "action": action, "started_at": now(),
                 "last_checked_at": old.get("last_checked_at"),
                 "message": "准备连接 NCBI 官方数据源"}
        write_json(state_path, state)
    except Exception:
        lock.close()
        raise

    def run():
        def report(message):
            state["message"] = message
            write_json(state_path, state)
        try:
            state.update(check_and_publish(action, report), status="succeeded")
        except Exception as exc:
            logger.exception("Knowledge update failed")
            state.update(status="failed", message=f"检查/更新失败，未替换为未校验数据：{exc}")
        finally:
            try:
                state["finished_at"] = now()
                write_json(state_path, state)
            finally:
                lock.close()
        return state

    if not background:
        return run()
    try:
        threading.Thread(target=run, name="knowledge-update", daemon=True).start()
    except Exception:
        lock.close()
        raise
    return dict(state)
