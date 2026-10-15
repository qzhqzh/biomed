"""Resolve immutable knowledge snapshots published by the updater."""

import json
import re
from pathlib import Path

from django.conf import settings


def updates_dir() -> Path:
    return Path(settings.KNOWLEDGE_UPDATES_DIR)


def active_dir() -> Path:
    pointer = updates_dir() / "current.json"
    if not pointer.is_file():
        return Path(settings.KNOWLEDGE_DIR)
    release = json.loads(pointer.read_text(encoding="utf-8"))["release"]
    if not re.fullmatch(r"[a-f0-9]{32}", release):
        raise ValueError("Invalid knowledge release identifier")
    return updates_dir() / "releases" / release
