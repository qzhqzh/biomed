"""Extend WhiteNoise's Django hash policy to manifest-listed Vite assets."""

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from whitenoise.middleware import WhiteNoiseMiddleware
from whitenoise.string_utils import ensure_leading_trailing_slash

VITE_ASSET = re.compile(
    r"assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8}\."
    r"(?:js|css|woff2?|ttf|otf|eot|png|jpe?g|gif|svg|webp|avif|ico|wasm)\Z"
)


def _static_prefix():
    prefix = getattr(settings, "WHITENOISE_STATIC_PREFIX", None)
    if prefix is None:
        prefix = urlparse(settings.STATIC_URL or "").path
        script_name = (settings.FORCE_SCRIPT_NAME or "").rstrip("/")
        if script_name and prefix.startswith(script_name):
            prefix = prefix[len(script_name) :]
    return ensure_leading_trailing_slash(prefix)


@lru_cache(maxsize=4)
def _manifest_assets(path, modified_ns, size):
    # mtime/size are cache keys: a local rebuild must not reuse an old allowlist.
    if size > 1024 * 1024:
        return frozenset()
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(manifest, dict):
        return frozenset()
    assets = set()
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        candidates = [entry.get("file")]
        for key in ("css", "assets"):
            if isinstance(entry.get(key), list):
                candidates.extend(entry[key])
        assets.update(
            name
            for name in candidates
            if isinstance(name, str) and VITE_ASSET.fullmatch(name)
        )
    return frozenset(assets)


class _DjangoHashPolicy(WhiteNoiseMiddleware):
    def __init__(self, prefix):
        # The inherited hash check needs only the prefix and storage helpers.
        # Do not initialise another middleware and recursively enumerate files.
        self.static_prefix = prefix


def immutable_file_test(path, url):
    """Used by WhiteNoise for static files; never cache an application response."""
    prefix = _static_prefix()
    parsed = urlparse(url)
    if (
        parsed.query
        or parsed.fragment
        or not url.startswith(prefix)
        or parsed.path.lower().endswith((".html", ".htm"))
        or Path(parsed.path).name.lower().startswith("manifest.")
    ):
        return False
    vite_prefix = prefix + "portal/dist/"
    if url.startswith(vite_prefix):
        name = url[len(vite_prefix) :]
        if VITE_ASSET.fullmatch(name):
            # collectstatic ignores .vite; the original build manifest remains
            # in the image beside Django's portal templates and source assets.
            manifest = (
                Path(settings.BASE_DIR)
                / "apps/portal/static/portal/dist/.vite/manifest.json"
            )
            try:
                info = manifest.stat()
            except OSError:
                pass
            else:
                if name in _manifest_assets(
                    str(manifest), info.st_mtime_ns, info.st_size
                ):
                    return True
    return _DjangoHashPolicy(prefix).immutable_file_test(path, url)
