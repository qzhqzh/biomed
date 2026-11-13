from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings


@override_settings(
    BIOMED_REQUIRE_LOGIN=True,
    BIOMED_ALL_AUTHENTICATED_CAN_REVIEW=True,
    ALLOWED_HOSTS=["testserver", "biomed.qzhqzh.com"],
    CSRF_TRUSTED_ORIGINS=["https://biomed.qzhqzh.com"],
)
class WorkbenchAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = get_user_model().objects.create_user(
            username="workbench-user", password="only-for-this-test"
        )
        self.client = Client(enforce_csrf_checks=True)

    def csrf(self, *, secure=False, host="testserver"):
        response = self.client.get("/accounts/login/", secure=secure, HTTP_HOST=host)
        self.assertEqual(response.status_code, 200)
        return self.client.cookies["csrftoken"].value

    def test_anonymous_pages_and_apis_require_login_but_health_is_available(self):
        response = self.client.get("/knowledge/candidates/?source_run=example")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/accounts/login/?next="))
        self.assertIn("no-store", response["Cache-Control"])
        for path in (
            "/api/v1/curation/materials/",
            "/api/v1/curation/session/",
            "/api/v1/annotate",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("请先登录", response.json()["error"])
        self.assertEqual(self.client.post("/api/v1/curation/materials/").status_code, 401)
        self.assertEqual(self.client.get("/health/live").status_code, 200)

    def test_public_login_csrf_session_access_and_post_logout(self):
        host = "biomed.qzhqzh.com"
        token = self.csrf(secure=True, host=host)
        data = {
            "username": self.user.username,
            "password": "only-for-this-test",
            "next": "/knowledge/candidates/?source_run=example",
            "csrfmiddlewaretoken": token,
        }
        bad_origin = self.client.post(
            "/accounts/login/", data, secure=True, HTTP_HOST=host,
            HTTP_ORIGIN="https://other.example",
        )
        self.assertEqual(bad_origin.status_code, 403)
        response = self.client.post(
            "/accounts/login/", data, secure=True, HTTP_HOST=host,
            HTTP_ORIGIN="https://biomed.qzhqzh.com",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/knowledge/candidates/?source_run=example")
        self.assertIn("sessionid", response.cookies)
        response = self.client.get("/", secure=True, HTTP_HOST=host)
        self.assertContains(response, 'data-biomed-user="workbench-user"')
        self.assertIn("no-store", response["Cache-Control"])
        session = self.client.get("/api/v1/curation/session/", secure=True, HTTP_HOST=host)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["username"], "workbench-user")
        self.assertTrue(session.json()["can_review"])
        self.assertTrue(self.user.has_perm("curation.change_evidence"))
        self.assertEqual(self.client.get("/accounts/logout/", secure=True, HTTP_HOST=host).status_code, 405)
        token = self.client.cookies["csrftoken"].value
        response = self.client.post(
            "/accounts/logout/", {"csrfmiddlewaretoken": token}, secure=True,
            HTTP_HOST=host, HTTP_ORIGIN="https://biomed.qzhqzh.com",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/v1/curation/session/", secure=True, HTTP_HOST=host).status_code, 401)

    def test_rejects_external_next_and_inactive_accounts(self):
        token = self.csrf()
        response = self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "only-for-this-test",
            "next": "https://elsewhere.example/", "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.url, "/")
        self.client.logout()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertFalse(self.user.has_perm("curation.change_evidence"))
        token = self.csrf()
        response = self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "only-for-this-test",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    @override_settings(BIOTOOLS_API_KEYS="", BIOTOOLS_REQUIRE_API_KEY=False, BIOTOOLS_JOB_COOLDOWN_SECONDS=10)
    def test_session_drf_writes_require_csrf_and_limit_each_user_separately(self):
        from apps.annotations.models import AnnotationJob

        def fake_create(**kwargs):
            return AnnotationJob(
                id="00000000-0000-0000-0000-000000000001",
                status=AnnotationJob.Status.QUEUED,
                assembly=kwargs["assembly"], engines=kwargs["engines"],
                variants=kwargs["variants"], variant_count=len(kwargs["variants"]),
            )

        other = get_user_model().objects.create_user("second-user", password="fixture")
        self.client.force_login(self.user)
        self.client.get("/")
        token = self.client.cookies["csrftoken"].value
        payload = {"engines": ["vep"], "assembly": "GRCh37", "variants": ["17:7577120 C>T"]}
        with patch("apps.annotations.services.jobs.create_and_enqueue", side_effect=fake_create):
            response = self.client.post("/api/v1/jobs/", payload, content_type="application/json")
            self.assertEqual(response.status_code, 403)
            first = self.client.post("/api/v1/jobs/", payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(first.status_code, 202)
            again = self.client.post("/api/v1/jobs/", payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(again.status_code, 429)
            self.client.force_login(other)
            second = self.client.post("/api/v1/jobs/", payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
            self.assertEqual(second.status_code, 202)

    def test_failed_login_limit_is_per_account(self):
        token = self.csrf()
        for _ in range(5):
            response = self.client.post("/accounts/login/", {
                "username": self.user.username, "password": "wrong",
                "csrfmiddlewaretoken": token,
            })
            self.assertEqual(response.status_code, 200)
        response = self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "wrong",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "登录尝试过多", status_code=429)
        response = self.client.post("/accounts/login/", {
            "username": "another-user", "password": "wrong",
            "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.status_code, 200)


@override_settings(BIOMED_REQUIRE_LOGIN=False, BIOMED_ALL_AUTHENTICATED_CAN_REVIEW=False)
class WorkbenchAccessCompatibilityTests(TestCase):
    def test_opt_out_preserves_existing_local_access_and_permissions(self):
        user = get_user_model().objects.create_user("legacy-user", password="fixture")
        self.assertFalse(user.has_perm("curation.change_evidence"))
        self.assertEqual(self.client.get("/").status_code, 200)
