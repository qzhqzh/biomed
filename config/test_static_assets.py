import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware

from config.static_assets import _manifest_assets, immutable_file_test


class StaticAssetCacheTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "apps/portal/static/portal/dist/.vite/manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "index.html": {
                        "file": "assets/biomed-Ab12_c-D.js",
                        "css": ["assets/index-1234ABcd.css"],
                        "assets": ["assets/font-ABcd1234.woff2", "assets/plain.js"],
                    }
                }
            )
        )
        context = override_settings(BASE_DIR=self.root, STATIC_URL="/static/")
        context.enable()
        self.addCleanup(context.disable)
        self.addCleanup(_manifest_assets.cache_clear)

    def test_only_listed_hashed_vite_files_receive_immutable_cache(self):
        for name in ["biomed-Ab12_c-D.js", "index-1234ABcd.css", "font-ABcd1234.woff2"]:
            self.assertTrue(
                immutable_file_test("unused", "/static/portal/dist/assets/" + name)
            )
        for url in [
            "/static/portal/dist/assets/unlisted-ABcd1234.js",
            "/static/portal/dist/assets/plain.js",
            "/static/portal/dist/.vite/manifest.json",
            "/static/manifest.abcd1234.json",
            "/static/portal/dist/index.html",
            "/static/portal/dist/assets/index-ABcd1234.html",
            "/api/v1/curation/materials/",
            "/management/research/",
            "/static/portal/dist/assets/biomed-Ab12_c-D.js?version=1",
        ]:
            self.assertFalse(immutable_file_test("unused", url), url)

    def test_missing_or_invalid_manifest_fails_closed_and_rebuild_updates(self):
        url = "/static/portal/dist/assets/biomed-Ab12_c-D.js"
        self.assertTrue(immutable_file_test("unused", url))
        self.manifest.write_text("{}")
        self.assertFalse(immutable_file_test("unused", url))
        self.manifest.write_text("invalid")
        self.assertFalse(immutable_file_test("unused", url))
        self.manifest.unlink()
        self.assertFalse(immutable_file_test("unused", url))

    def test_django_hashed_files_keep_whitenoise_storage_verification(self):
        url = "/static/admin/css/base.abcd1234.css"
        with patch.object(
            WhiteNoiseMiddleware, "get_static_url", return_value=url
        ) as storage_url:
            self.assertTrue(immutable_file_test("unused", url))
            storage_url.assert_called_once_with("admin/css/base.css")
        with patch.object(
            WhiteNoiseMiddleware,
            "get_static_url",
            return_value="/static/admin/css/base.css",
        ):
            self.assertFalse(immutable_file_test("unused", url))
        vite_django_hash = "/static/portal/dist/assets/app.abcd1234.css"
        with patch.object(
            WhiteNoiseMiddleware, "get_static_url", return_value=vite_django_hash
        ):
            self.assertTrue(immutable_file_test("unused", vite_django_hash))

    def test_actual_whitenoise_headers_only_mark_hashed_asset_immutable(self):
        static_root = self.root / "collected"
        asset = static_root / "portal/dist/assets/biomed-Ab12_c-D.js"
        asset.parent.mkdir(parents=True)
        asset.write_text("console.log('test');")
        html = static_root / "portal/dist/index.html"
        html.write_text("<html>test</html>")
        with override_settings(
            DEBUG=False,
            STATIC_ROOT=static_root,
            WHITENOISE_USE_FINDERS=False,
            WHITENOISE_IMMUTABLE_FILE_TEST=immutable_file_test,
        ):
            middleware = WhiteNoiseMiddleware(
                lambda request: HttpResponse("application")
            )
            factory = RequestFactory()
            response = middleware(
                factory.get("/static/portal/dist/assets/biomed-Ab12_c-D.js")
            )
            self.assertIn("immutable", response["Cache-Control"])
            response.close()
            response = middleware(factory.get("/static/portal/dist/index.html"))
            self.assertNotIn("immutable", response["Cache-Control"])
            response.close()
            response = middleware(factory.get("/api/v1/curation/materials/"))
            self.assertNotIn("Cache-Control", response)
