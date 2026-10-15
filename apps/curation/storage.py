"""Content-addressed immutable blobs. Database versions refer to SHA-256, never user paths."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from django.conf import settings


def blob_path(digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("无效的资料校验值")
    return Path(settings.CURATION_STORAGE_DIR) / "blobs" / digest[:2] / digest


def store_blob(content):
    digest = hashlib.sha256(content).hexdigest()
    target = blob_path(digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("已有原始文件校验失败，已停止写入")
        return digest
    fd, name = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o640)
        # Atomic rename; concurrent writers of the same digest have identical content.
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return digest


def read_blob(digest):
    content = blob_path(digest).read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("原始文件校验失败")
    return content
