"""Only server-built requests to configured public providers; no arbitrary URL ingestion."""

import gzip
import io
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from defusedxml import ElementTree

MAX_BYTES = 128 * 1024 * 1024
ALLOWED_HOSTS = frozenset(
    {
        "eutils.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "rxnav.nlm.nih.gov",
        "api.github.com",
        "github.com",
        "release-assets.githubusercontent.com",
    }
)


class SourceBlocked(ValueError):
    pass


class TransientSourceError(Exception):
    pass


def allowed(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise SourceBlocked("数据源地址不在允许的官方来源列表中")


class SourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit=MAX_BYTES):
    allowed(url)
    # Worker is serial, including NCBI acquisition; remain below the unkeyed 3 rps limit.
    time.sleep(0.4)
    request = Request(
        url,
        headers={
            "User-Agent": "biomed/1.0 (source-ingestion)",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with build_opener(SourceRedirect()).open(request, timeout=60) as response:
            data = response.read(limit + 1)
            encoding = response.headers.get("Content-Encoding", "")
            if len(data) > limit:
                raise SourceBlocked("来源文件超过下载上限")
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    data = stream.read(limit + 1)
            elif encoding == "deflate":
                import zlib

                decoder = zlib.decompressobj()
                data = decoder.decompress(data, limit + 1)
                if decoder.unconsumed_tail:
                    raise SourceBlocked("来源文件解压后超过上限")
            if len(data) > limit:
                raise SourceBlocked("来源文件解压后超过上限")
            return data
    except HTTPError as exc:
        if exc.code in (429, 500, 502, 503, 504):
            raise TransientSourceError(f"官方来源暂不可用（HTTP {exc.code}）") from exc
        raise SourceBlocked(f"官方来源拒绝获取（HTTP {exc.code}）") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TransientSourceError("官方来源连接失败，请稍后重试") from exc


def fetch_json(url):
    return json.loads(fetch(url))


def xml(content):
    # defusedxml rejects external entities and entity expansion.
    return ElementTree.fromstring(content)


def text(node):
    return " ".join("".join(node.itertext()).split()) if node is not None else ""
