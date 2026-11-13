import json
import re

from django.test import SimpleTestCase

from apps.portal.templatetags.vite import MANIFEST


class UnifiedFrontendTests(SimpleTestCase):
    def test_routes_render_one_manifest_entry_without_duplicate_query_identity(self):
        entry = json.loads(MANIFEST.read_text())["index.html"]
        for path, root in [
            ("/", "home-root"),
            ("/evidence/sources/", "library-root"),
            ("/evidence/sources/papers/", "library-root"),
            ("/evidence/sources/guidelines/", "library-root"),
            ("/evidence/clinical/", "clinical-evidence-root"),
            ("/knowledge/diseases/", "standards-root"),
            ("/knowledge/drugs/", "standards-root"),
            ("/knowledge/candidates/", "knowledge-candidates-root"),
            ("/knowledge/entities/", "entity-registry-root"),
            ("/knowledge/search/", "knowledge-search-root"),
            ("/management/workflows/", "workflows-root"),
            ("/management/research/", "research-root"),
            ("/library/", "library-root"),
            ("/workflows/", "workflows-root"),
            ("/knowledge/standards/", "standards-root"),
            ("/knowledge/evidence/", "clinical-evidence-root"),
            ("/tools/teaching/", "evidence-root"),
        ]:
            response = self.client.get(path)
            self.assertContains(response, root)
            self.assertIn("csrftoken", response.cookies)
            scripts = re.findall(
                r'<script type="module" src="([^"]+)"', response.content.decode()
            )
            self.assertEqual(scripts, ["/static/portal/dist/" + entry["file"]])
