import io
import json
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import Client, SimpleTestCase, override_settings

from apps.annotations.knowledge import store


def response(data, code=200):
    result = io.BytesIO(json.dumps(data).encode())
    result.code = code
    result.headers = Message()
    result.headers["Content-Type"] = "application/json"
    return result


@override_settings(EVIDENCE_API_URL="http://oncology:8000")
class EvidenceBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/tools/teaching/")
        self.token = self.client.cookies["csrftoken"].value
        self.forward = patch("apps.evidence.views._opener.open")
        self.upstream = self.forward.start()
        self.addCleanup(self.forward.stop)

    def test_shared_pages_and_portal_metadata(self):
        for path, root in [
            ("/", "home"),
            ("/tools/teaching/", "evidence"),
            ("/knowledge/issues/", "issues"),
        ]:
            page = self.client.get(path)
            self.assertContains(page, f'id="{root}-root"')
            self.assertContains(page, "biomed")
            self.assertNotContains(page, "18765")
        self.assertEqual(
            self.client.get("/", HTTP_ACCEPT="application/json").json()["service"],
            "biomed",
        )

    def test_csrf_token_and_origin_required_for_writes(self):
        payload = {"fields": {"cancer": "肺腺癌", "stage": ""}, "confirmed": True}
        path = "/api/v1/evidence/search/"
        self.assertEqual(
            self.client.post(
                path, payload, content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                path,
                payload,
                content_type="application/json",
                HTTP_X_CSRFTOKEN=self.token,
                HTTP_ORIGIN="https://unrelated.example",
            ).status_code,
            403,
        )
        self.upstream.assert_not_called()
        self.upstream.return_value = response({"status": "matched", "results": []})
        result = self.client.post(
            path,
            payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
            HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(result.status_code, 200)
        request = self.upstream.call_args.args[0]
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.full_url, "http://oncology:8000/api/search")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("X-csrftoken", request.headers)
        self.assertIn("no-store", result["Cache-Control"])

    def test_only_known_routes_methods_and_small_json_bodies(self):
        self.assertEqual(self.client.get("/api/v1/evidence/search/").status_code, 405)
        self.assertEqual(self.client.get("/api/v1/evidence/unknown/").status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/evidence/issues/not-an-id/").status_code, 404
        )
        headers = {"HTTP_X_CSRFTOKEN": self.token}
        self.assertEqual(
            self.client.post(
                "/api/v1/evidence/extract/",
                "x" * 32769,
                content_type="application/json",
                **headers,
            ).status_code,
            413,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/evidence/extract/",
                "test",
                content_type="text/plain",
                **headers,
            ).status_code,
            415,
        )
        self.upstream.assert_not_called()

    def test_service_validation_errors_are_preserved(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        payload = {"detail": [{"loc": ["body", "confirmed"], "msg": "Must confirm"}]}
        self.upstream.side_effect = HTTPError(
            "http://oncology:8000/api/search",
            422,
            "Invalid",
            headers,
            io.BytesIO(json.dumps(payload).encode()),
        )
        result = self.client.post(
            "/api/v1/evidence/search/",
            {"fields": {}, "confirmed": False},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json(), payload)

    def test_export_preserves_all_records_and_download_name(self):
        payload = {"schema_version": "1", "items": [{"id": str(i)} for i in range(202)]}
        self.upstream.return_value = response(payload)
        result = self.client.get("/api/v1/evidence/issues/export/")
        self.assertEqual(result.json(), payload)
        self.assertEqual(
            result["Content-Disposition"], 'attachment; filename="learning-log.json"'
        )
        self.assertEqual(
            self.upstream.call_args.args[0].full_url,
            "http://oncology:8000/api/issues/export",
        )

    def test_timeout_redirect_and_invalid_data_fail_without_fake_results(self):
        self.upstream.side_effect = URLError("private-network-details")
        result = self.client.get("/api/v1/evidence/catalog/")
        self.assertEqual(result.status_code, 503)
        self.assertNotContains(result, "private-network-details", status_code=503)
        self.upstream.side_effect = None
        for code, content in [(302, {}), (500, {"trace": "secret"})]:
            self.upstream.return_value = response(content, code)
            self.assertEqual(
                self.client.get("/api/v1/evidence/catalog/").status_code, 503
            )
        self.upstream.return_value = response({})
        self.upstream.return_value.headers.replace_header("Content-Type", "text/html")
        self.assertEqual(self.client.get("/api/v1/evidence/catalog/").status_code, 503)


class AccessionGeneTests(SimpleTestCase):
    def setUp(self):
        snapshot = store._build_snapshot(
            Path("/unused"),
            [
                {"gene_id": 1956, "symbol": "EGFR", "type": "protein-coding"},
                {"gene_id": 19560, "symbol": "EGFR-AS1", "type": "ncRNA"},
            ],
            {
                "NM_005228": {"gene": "EGFR", "mane": True},
                "ENST00000275493": {"gene": "EGFR", "mane": True},
            },
            {},
        )
        self.loaded = patch.object(store, "ensure_loaded", return_value=snapshot)
        self.loaded.start()
        self.addCleanup(self.loaded.stop)

    def test_accession_resolves_exact_gene_without_asserting_version(self):
        for query in ["NM_005228", "nm005228.999", "ENST00000275493.999"]:
            result = store.search_genes(query)
            self.assertEqual([row["symbol"] for row in result["results"]], ["EGFR"])
            self.assertFalse(result["query_resolution"]["version_checked"])
            self.assertEqual(result["query_resolution"]["requested"], query)
        self.assertEqual(store.search_genes("NM_999999")["results"], [])

    def test_accession_pagination_type_filter_and_existing_gene_search(self):
        self.assertEqual(store.search_genes("NM_005228", offset=1)["results"], [])
        self.assertEqual(
            store.search_genes("NM_005228", gene_type="ncRNA")["results"], []
        )
        self.assertEqual(store.search_genes("EGFR")["results"][0]["symbol"], "EGFR")
