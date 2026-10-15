"""Render the built entry exactly once, including its content-hashed assets."""

import json
from pathlib import Path

from django import template
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join

register = template.Library()
MANIFEST = Path(__file__).parents[1] / "static/portal/dist/.vite/manifest.json"


@register.simple_tag
def vite_assets():
    entry = json.loads(MANIFEST.read_text())["index.html"]
    styles = format_html_join(
        "\n",
        '<link rel="stylesheet" href="{}">',
        ((static(f"portal/dist/{path}"),) for path in entry.get("css", [])),
    )
    return format_html(
        '{}\n<script type="module" src="{}"></script>',
        styles,
        static(f"portal/dist/{entry['file']}"),
    )
