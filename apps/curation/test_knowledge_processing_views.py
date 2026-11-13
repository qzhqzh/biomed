import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.curation.knowledge_processing_views import status
from apps.curation.views import overview


class ProcessingStatusTests(SimpleTestCase):
    def test_workflow_runtime_does_not_count_the_literature_tables(self):
        with patch("apps.curation.views.WorkerHeartbeat.objects.filter") as heartbeat:
            heartbeat.return_value.first.return_value = None
            response = overview(RequestFactory().get("/api/v1/curation/overview/?runtime=1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(json.loads(response.content)), {"worker_online", "model_configured", "publication_policy"})

    def test_live_stale_missing_status_without_database_access(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(KNOWLEDGE_PROCESSING_ROOT=folder):
            request = RequestFactory().get("/api/v1/curation/knowledge-processing/")
            self.assertFalse(json.loads(status(request).content)["online"])
            path = Path(folder) / "status.json"
            path.write_text(json.dumps({"updated_at": time.time(), "activity": "running"}))
            self.assertTrue(json.loads(status(request).content)["online"])
            path.write_text(json.dumps({"updated_at": time.time()-1000, "activity": "running"}))
            self.assertFalse(json.loads(status(request).content)["online"])
            self.assertEqual(status(RequestFactory().post("/api/v1/curation/knowledge-processing/")).status_code, 405)
