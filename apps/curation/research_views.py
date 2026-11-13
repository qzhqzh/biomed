"""Research history uses no ORM calls and cannot start collection or inference."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.curation import research_history


@require_GET
def history(request, record_id=None):
    try:
        payload = (
            research_history.public_record(research_history.read_record(record_id))
            if record_id is not None
            else research_history.records_index()
        )
        response = JsonResponse(payload)
    except FileNotFoundError:
        response = JsonResponse({"error": "未找到研究记录。"}, status=404)
    except (ValueError, KeyError, TypeError, OSError):
        response = JsonResponse({"error": "研究记录暂不可用，请检查已登记文件。"}, status=503)
    response["Cache-Control"] = "no-store"
    return response
