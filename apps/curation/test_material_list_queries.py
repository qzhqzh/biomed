"""Cold material-list reads stay on the small index, even before UI caches warm."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.curation.models import Material


class MaterialListQueryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.materials = []
        for index, kind in enumerate(("paper", "guideline", "paper", "other", "guideline")):
            material = Material.objects.create(
                source_key=f"fixture:{index}", kind=kind, title=f"Document {index}",
                metadata={"abstract": "Text that must stay outside listing sorts."},
            )
            Material.objects.filter(pk=material.pk).update(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
            )
            self.materials.append(material)

    def explain(self, sql):
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql)
            return " ".join(str(row) for row in cursor.fetchall()).upper()

    def test_cold_counts_and_each_category_page_need_no_temporary_sort(self):
        for kind in ("", "paper", "guideline", "other"):
            with self.subTest(kind=kind):
                cache.clear()
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(
                        "/api/v1/curation/materials/",
                        {"summary": "1", "kind": kind, "limit": 1},
                    )
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result["counts"],
                                 {"paper": 2, "guideline": 2, "other": 1, "all": 5})
                expected = next(m for m in reversed(self.materials) if not kind or m.kind == kind)
                self.assertEqual(result["results"][0]["id"], str(expected.pk))
                count_sql = next(q["sql"] for q in queries if 'AS "paper"' in q["sql"])
                page_sql = next(q["sql"] for q in queries
                                if 'ORDER BY "curation_material"."created_at" DESC' in q["sql"])
                self.assertNotIn("GROUP BY", count_sql.upper())
                for sql in (count_sql, page_sql):
                    plan = self.explain(sql)
                    self.assertIn("COVERING INDEX MATERIAL_LIST_CREATED", plan)
                    self.assertNotIn("TEMP B-TREE", plan)

    def test_offset_keeps_latest_created_order(self):
        for kind, expected in (("", self.materials[-2]), ("paper", self.materials[0]),
                               ("guideline", self.materials[1])):
            with self.subTest(kind=kind):
                result = self.client.get(
                    "/api/v1/curation/materials/",
                    {"summary": "1", "kind": kind, "limit": 1, "offset": 1},
                ).json()
                self.assertEqual(result["results"][0]["id"], str(expected.pk))

    def test_counts_and_page_cache_both_survive_thirty_seconds(self):
        params = {"summary": "1", "kind": "paper", "limit": 1}
        initial = self.client.get("/api/v1/curation/materials/", params).json()
        later = time.time() + 31
        with patch("django.core.cache.backends.locmem.time.time", return_value=later):
            with CaptureQueriesContext(connection) as queries:
                result = self.client.get("/api/v1/curation/materials/", params).json()
        self.assertEqual(result, initial)
        self.assertEqual(len(queries), 2)
        self.assertNotIn('AS "paper"', queries[0]["sql"])
        self.assertFalse(any("ORDER BY" in q["sql"] for q in queries))
