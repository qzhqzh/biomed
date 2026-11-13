"""Serial, measured official requests with persistent conservative budget accounting."""

import gzip
import io
import logging
import os
import re
import shutil
import time
import zlib
from datetime import timedelta
from datetime import timezone as datetime_timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.curation.models import CollectionFetch, LiteratureCollection, WorkItem
from apps.curation.network import SourceBlocked, TransientSourceError, allowed
from apps.curation.storage import store_blob
from config.database import safe_failure_location

logger = logging.getLogger(__name__)


class CollectionStopped(SourceBlocked):
    pass


class LeaseLost(Exception):
    pass


class CollectionRetry(TransientSourceError):
    def __init__(self, message, retry_after=60, *, scheduling_wait=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.scheduling_wait = scheduling_wait


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Each ledger entry must correspond to one request. Never follow unmetered redirects.
        raise SourceBlocked("官方端点发生重定向，需核对后更新采集地址")


def check_lease(item):
    if not WorkItem.objects.filter(
        pk=item.pk,
        state="running",
        lease_token=item.lease_token,
        lease_until__gt=timezone.now(),
    ).exists():
        raise LeaseLost()


def retry_seconds(value):
    try:
        seconds = max(30, int(value))
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime_timezone.utc)
            seconds = max(30, (stamp - timezone.now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 30
    if seconds > 7 * 24 * 3600:
        # Do not overflow datetime or silently retry before the source permits it.
        raise CollectionStopped("来源要求等待超过 7 天，已暂停；请核对来源要求后续跑")
    return seconds


def retryable_http(url, status):
    if status in (408, 429, 500, 502, 503, 504):
        return True
    # Real PubMed EFetch pages have intermittently returned 400 then succeeded.
    # Do not retry invalid searches, permission denials or PMC licensing errors.
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    ids = params.get("id", [""])[0].split(",")
    return (
        status == 400
        and parsed.hostname == "eutils.ncbi.nlm.nih.gov"
        and parsed.path == "/entrez/eutils/efetch.fcgi"
        and params.get("db") == ["pubmed"]
        and 1 <= len(ids) <= 100
        and all(re.fullmatch(r"\d{1,10}", pmid) for pmid in ids)
    )


def pmc_off_peak_delay(now):
    """Seconds until PMC permits a bulk script; use Eastern time including DST."""
    try:
        eastern = now.astimezone(ZoneInfo("America/New_York"))
        if eastern.weekday() >= 5 or not 5 <= eastern.hour < 21:
            return 0
        permitted = eastern.replace(hour=21, minute=0, second=0, microsecond=0)
    except ZoneInfoNotFoundError:
        # Minimal runtime images may not ship the IANA database. UTC 02:00–09:00
        # is outside both EST and EDT peak hours; otherwise conservatively wait.
        utc = now.astimezone(datetime_timezone.utc)
        if 2 <= utc.hour < 9:
            return 0
        permitted = utc.replace(hour=2, minute=0, second=0, microsecond=0)
        if utc.hour >= 9:
            permitted += timedelta(days=1)
    return (
        permitted.astimezone(datetime_timezone.utc)
        - now.astimezone(datetime_timezone.utc)
    ).total_seconds()


def pmc_peak_hours(now):
    return pmc_off_peak_delay(now) > 0


class CollectionHTTP:
    def __init__(self, item):
        self.item = item

    def fetch(self, url, limit=8 * 1024 * 1024):
        allowed(url)
        check_lease(self.item)
        collection_id = self.item.payload["collection_id"]
        run = LiteratureCollection.objects.get(pk=collection_id)
        last = (
            CollectionFetch.objects.order_by("-started_at")
            .values_list("started_at", flat=True)
            .first()
        )
        if last:
            time.sleep(
                max(
                    0,
                    run.limits["interval_seconds"]
                    - (timezone.now() - last).total_seconds(),
                )
            )
        with transaction.atomic():
            check_lease(self.item)
            run = LiteratureCollection.objects.select_for_update().get(pk=collection_id)
            if run.step != self.item.payload["step"]:
                raise LeaseLost()
            if run.state not in ("estimating", "running"):
                raise CollectionStopped("已暂停；没有启动新的下载")
            usage = run.fetches.aggregate(total=Sum("charged_bytes"), requests=Count("id"))
            used = usage["total"] or 0
            if usage["requests"] >= run.limits["max_requests"]:
                raise CollectionStopped("达到本批请求次数上限；调整预算后可续跑")
            remaining = run.limits["max_bytes"] - used
            if remaining <= 0:
                raise CollectionStopped("达到本批累计响应字节上限；调整预算后可续跑")
            if shutil.disk_usage(settings.CURATION_STORAGE_DIR).free < 1024**3:
                raise CollectionStopped("资料磁盘可用空间少于 1 GiB，已暂停")
            for path in (
                settings.CURATION_STORAGE_DIR,
                Path(settings.DATABASES["default"]["NAME"]).parent,
            ):
                stats = os.statvfs(path)
                if stats.f_files and stats.f_favail < 4096:
                    raise CollectionStopped(
                        "资料或数据库磁盘剩余 inode 少于 4096，已暂停保留断点"
                    )
            if urlsplit(url).hostname == "pmc.ncbi.nlm.nih.gov":
                now = timezone.now()
                wait = pmc_off_peak_delay(now)
                if wait and (
                    run.limits.get("max_fulltexts", 0) > 100
                    or CollectionFetch.objects.filter(
                        url__startswith="https://pmc.ncbi.nlm.nih.gov/",
                        started_at__gte=now - timedelta(hours=24),
                    ).count()
                    >= 100
                ):
                    # Reserve neither requests nor bytes while waiting. The
                    # workflow persists this delay and resumes without a person.
                    raise CollectionRetry(
                        "PMC 批量请求须在美国东部时间非高峰进行；已保留断点，"
                        "将在下一个允许窗口自动继续",
                        retry_after=wait,
                        scheduling_wait=True,
                    )
            limit = min(limit, remaining)
            receipt = CollectionFetch.objects.create(
                collection=run, url=url, charged_bytes=limit
            )
        started = time.monotonic()
        wire = 0
        decoded = 0
        status = None
        digest = ""
        error = ""
        complete = False
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "biomed/1.0 (bounded-literature-ingestion)",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            with build_opener(NoRedirect()).open(request, timeout=60) as response:
                status = response.status
                chunks = []
                while True:
                    if wire >= limit:
                        raise CollectionStopped("单次响应达到字节上限，断点已保留")
                    if time.monotonic() - started > 180:
                        raise CollectionRetry("单次响应超过 180 秒，断点已保留")
                    chunk = response.read(min(16384, limit - wire))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    wire += len(chunk)
                    # Application read-rate ceiling, not an OS-level traffic shaper.
                    time.sleep(
                        max(
                            0,
                            wire / run.limits["bytes_per_second"]
                            - (time.monotonic() - started),
                        )
                    )
                data = b"".join(chunks)
                encoding = response.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                        data = stream.read(limit + 1)
                elif encoding == "deflate":
                    decoder = zlib.decompressobj()
                    data = decoder.decompress(data, limit + 1)
                    if decoder.unconsumed_tail:
                        raise CollectionStopped("来源响应解压后超过上限")
                    if not decoder.eof:
                        raise CollectionRetry("来源压缩响应不完整，断点已保留")
                elif encoding not in ("", "identity"):
                    raise SourceBlocked("来源返回不支持的压缩格式")
                decoded = len(data)
                if decoded > limit:
                    raise CollectionStopped("响应解压后超过本次上限，未保存不完整资料")
                try:
                    digest = store_blob(data)
                except OSError as exc:
                    logger.warning(
                        "Collection storage failure: %s", safe_failure_location(exc)
                    )
                    raise CollectionRetry("原始资料暂时无法保存，断点已保留") from exc
                check_lease(self.item)
                complete = True
                return data
        except HTTPError as exc:
            status = exc.code
            error = f"官方来源返回 HTTP {exc.code}"
            if retryable_http(url, exc.code):
                raise CollectionRetry(
                    error, retry_seconds(exc.headers.get("Retry-After"))
                ) from exc
            raise SourceBlocked(error) from exc
        except (
            URLError,
            TimeoutError,
            OSError,
            IncompleteRead,
            RemoteDisconnected,
            EOFError,
            zlib.error,
        ) as exc:
            logger.warning(
                "Collection connection/read failure: %s; reason_type=%s",
                safe_failure_location(exc),
                type(getattr(exc, "reason", None)).__name__,
            )
            error = "官方来源连接中断，断点已保留"
            raise CollectionRetry(error) from exc
        except CollectionRetry as exc:
            error = str(exc)
            raise
        except SourceBlocked as exc:
            error = str(exc)
            raise
        finally:
            # A killed process never reaches here: its pre-I/O reservation remains charged.
            CollectionFetch.objects.filter(pk=receipt.pk).update(
                state="succeeded" if complete else "failed",
                http_status=status,
                wire_bytes=wire,
                decoded_bytes=decoded,
                charged_bytes=max(wire, decoded)
                if complete or status in (429, 500, 502, 503, 504)
                else limit,
                elapsed_seconds=time.monotonic() - started,
                response_sha256=digest,
                message=error,
                finished_at=timezone.now(),
            )
