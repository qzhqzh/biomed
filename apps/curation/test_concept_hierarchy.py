from django.test import TestCase

from apps.curation.concept_hierarchy import release_graph
from apps.curation.models import Concept, EntityTranslation, SourceRelease


class ConceptHierarchyTests(TestCase):
    def setUp(self):
        release_graph.cache_clear()
        self.addCleanup(release_graph.cache_clear)
        self.release = SourceRelease.objects.create(
            namespace="MONDO",
            version="test",
            sha256="1" * 64,
            count=5,
            active=True,
        )
        for identifier, label, parents in [
            ("MONDO:1", "Root", []),
            ("MONDO:2", "Branch A", ["MONDO:1"]),
            ("MONDO:3", "Branch B", ["MONDO:1"]),
            ("MONDO:4", "Shared child", ["MONDO:2", "MONDO:3"]),
            ("MONDO:5", "Unresolved", ["OTHER:1"]),
        ]:
            Concept.objects.create(
                release=self.release,
                identifier=identifier,
                label=label,
                parents=parents,
            )

    def get(self, **kwargs):
        return self.client.get("/api/v1/curation/concepts/", kwargs)

    def test_roots_and_multi_parent_navigation_do_not_flatten_graph(self):
        data = self.get(parent="__roots__").json()
        self.assertEqual(
            [r["identifier"] for r in data["results"]], ["MONDO:1", "MONDO:5"]
        )
        for parent in ("MONDO:2", "MONDO:3"):
            data = self.get(parent=parent).json()
            self.assertEqual([r["identifier"] for r in data["results"]], ["MONDO:4"])
        data = self.get(parent="MONDO:4").json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["navigation"]["parents"]), 2)

    def test_external_parent_retained_for_local_entry_point(self):
        data = self.get(parent="MONDO:5").json()
        self.assertEqual(data["navigation"]["missing_parents"], ["OTHER:1"])
        self.assertEqual(data["navigation"]["parents"], [])

    def test_navigation_pins_release_and_rejects_cross_namespace(self):
        self.release.active = False
        self.release.save(update_fields=["active"])
        current = SourceRelease.objects.create(
            namespace="MONDO", version="new", sha256="2" * 64, count=1, active=True
        )
        Concept.objects.create(release=current, identifier="MONDO:1", label="New")
        data = self.get(parent="MONDO:1", release=str(self.release.pk)).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["release"]["version"], "test")
        self.assertEqual(self.get(parent="MONDO:1").json()["total"], 0)
        self.assertEqual(
            self.get(namespace="RXNORM", release=str(self.release.pk)).status_code, 404
        )

    def test_branch_search_and_pagination_are_scoped(self):
        data = self.get(parent="MONDO:1", q="Branch", limit=1, offset=1).json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["results"][0]["identifier"], "MONDO:3")
        self.assertEqual(self.get(parent="MONDO:1", q="Shared").json()["total"], 0)
        self.assertEqual(self.get(q="Shared").json()["total"], 1)

    def test_unknown_node_is_error_and_cycles_do_not_recurse(self):
        self.assertEqual(self.get(parent="MONDO:missing").status_code, 400)
        Concept.objects.filter(release=self.release, identifier="MONDO:1").update(
            parents=["MONDO:4"]
        )
        release_graph.cache_clear()
        self.assertEqual(self.get(parent="MONDO:4").json()["total"], 1)


class CancerTreeTests(TestCase):
    def setUp(self):
        release_graph.cache_clear()
        self.addCleanup(release_graph.cache_clear)
        self.release = SourceRelease.objects.create(
            namespace="ONCOTREE",
            version="tree-test",
            sha256="3" * 64,
            count=5,
            active=True,
        )
        for identifier, label, parents in [
            ("ONCOTREE:TISSUE", "Tissue", []),
            ("ONCOTREE:LUNG", "Lung", ["ONCOTREE:TISSUE"]),
            ("ONCOTREE:NSCLC", "Non-Small Cell Lung Cancer", ["ONCOTREE:LUNG"]),
            ("ONCOTREE:LUAD", "Lung Adenocarcinoma", ["ONCOTREE:NSCLC"]),
            ("ONCOTREE:BREAST", "Breast", ["ONCOTREE:TISSUE"]),
        ]:
            Concept.objects.create(
                release=self.release,
                identifier=identifier,
                label=label,
                parents=parents,
            )

    def get(self, **kwargs):
        return self.client.get(
            "/api/v1/curation/concepts/", {"namespace": "ONCOTREE", **kwargs}
        )

    def test_direct_child_counts_are_not_descendant_or_search_counts(self):
        data = self.get(parent="ONCOTREE:TISSUE", q="Lung", limit=1).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["child_count"], 1)
        self.assertNotIn("tree_path", data)
        leaf = self.get(q="ONCOTREE:LUAD").json()["results"][0]
        self.assertEqual(leaf["child_count"], 0)

    def test_path_uses_full_release_even_when_parent_and_search_exclude_target(self):
        data = self.get(
            parent="ONCOTREE:BREAST", q="unmatched", tree_path="ONCOTREE:LUAD"
        ).json()
        self.assertEqual(data["results"], [])
        self.assertEqual(
            data["tree_path"],
            ["ONCOTREE:TISSUE", "ONCOTREE:LUNG", "ONCOTREE:NSCLC", "ONCOTREE:LUAD"],
        )
        self.assertEqual(
            self.get(tree_path="ONCOTREE:TISSUE").json()["tree_path"],
            ["ONCOTREE:TISSUE"],
        )

    def test_path_and_counts_are_isolated_by_pinned_release(self):
        self.release.active = False
        self.release.save(update_fields=["active"])
        current = SourceRelease.objects.create(
            namespace="ONCOTREE",
            version="tree-new",
            sha256="4" * 64,
            count=2,
            active=True,
        )
        Concept.objects.create(
            release=current, identifier="ONCOTREE:TISSUE", label="Root"
        )
        Concept.objects.create(
            release=current,
            identifier="ONCOTREE:LUAD",
            label="New leaf",
            parents=["ONCOTREE:TISSUE"],
        )
        for release_id, expected_count, expected_path in [
            (
                self.release.pk,
                2,
                ["ONCOTREE:TISSUE", "ONCOTREE:LUNG", "ONCOTREE:NSCLC", "ONCOTREE:LUAD"],
            ),
            (current.pk, 1, ["ONCOTREE:TISSUE", "ONCOTREE:LUAD"]),
        ]:
            data = self.get(
                release=str(release_id), q="ONCOTREE:TISSUE", tree_path="ONCOTREE:LUAD"
            ).json()
            self.assertEqual(data["tree_path"], expected_path)
            self.assertEqual(data["results"][0]["child_count"], expected_count)

    def test_large_branch_is_complete_across_default_pages(self):
        Concept.objects.bulk_create(
            [
                Concept(
                    release=self.release,
                    identifier=f"ONCOTREE:B{i:02d}",
                    label=f"Breast subtype {i:02d}",
                    parents=["ONCOTREE:BREAST"],
                )
                for i in range(63)
            ]
        )
        counts = self.get(q="ONCOTREE:BREAST").json()["results"][0]
        self.assertEqual(counts["child_count"], 63)
        first = self.get(parent="ONCOTREE:BREAST").json()
        second = self.get(parent="ONCOTREE:BREAST", offset=50).json()
        self.assertEqual(
            (first["total"], first["limit"], len(first["results"])), (63, 50, 50)
        )
        self.assertEqual(len(second["results"]), 13)
        identifiers = {
            row["identifier"] for row in first["results"] + second["results"]
        }
        self.assertEqual(len(identifiers), 63)
        self.assertTrue(all(row["child_count"] == 0 for row in second["results"]))

    def test_chinese_search_and_navigation_keep_translations(self):
        lung = Concept.objects.get(release=self.release, identifier="ONCOTREE:LUNG")
        EntityTranslation.objects.create(
            concept=lung,
            label="肺",
            provider="test",
            status="reference",
            source_version="test",
            match_basis="test",
            content_sha256="5" * 64,
        )
        result = self.get(q="肺").json()["results"][0]
        self.assertEqual(result["child_count"], 1)
        self.assertEqual(result["translations"][0]["label"], "肺")
        navigation = self.get(parent="ONCOTREE:LUNG").json()["navigation"]
        self.assertEqual(navigation["focus"]["translations"][0]["label"], "肺")

    def test_unknown_cyclic_external_and_multi_parent_paths_are_rejected(self):
        self.assertEqual(self.get(tree_path="ONCOTREE:missing").status_code, 400)
        self.assertEqual(self.get(tree_path="").status_code, 400)
        self.assertEqual(
            self.get(namespace="MONDO", tree_path="MONDO:1").status_code, 400
        )
        for parents in [
            ["ONCOTREE:LUAD"],
            ["ONCOTREE:missing"],
            ["ONCOTREE:LUNG", "ONCOTREE:BREAST"],
        ]:
            with self.subTest(parents=parents):
                Concept.objects.filter(
                    release=self.release, identifier="ONCOTREE:LUAD"
                ).update(parents=parents)
                release_graph.cache_clear()
                self.assertEqual(self.get(tree_path="ONCOTREE:LUAD").status_code, 400)
