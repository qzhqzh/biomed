"""Read the worker's atomic status file; never poll the literature database."""

import json
import time
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def status(request):
    root = getattr(settings, "KNOWLEDGE_PROCESSING_ROOT", "/app/knowledge-processing")
    path = Path(root) / "status.json"
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Oversized status")
        data = json.loads(path.read_text())
        data["online"] = time.time() - float(data["updated_at"]) < 90 and data["activity"] != "stopped"
    except (OSError, ValueError, KeyError, TypeError):
        data = {"online": False, "activity": "unavailable", "reason": "尚无可用知识服务状态，资料采集独立运行。"}
    response = JsonResponse(data)
    response["Cache-Control"] = "no-store"
    return response
