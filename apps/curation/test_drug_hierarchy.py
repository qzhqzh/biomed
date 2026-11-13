import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from apps.curation.drug_hierarchy import drug_tree
from apps.curation.models import (
    Concept,
    EntityTranslation,
    ResourceAssertion,
    ResourceDataset,
    ResourceEntity,
    SourceRelease,
)


class DrugHierarchyTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.release = SourceRelease.objects.create(
            namespace="RXNORM", version="rx-v1", sha256="a" * 64, count=2, active=True
        )
        self.drug = Concept.objects.create(
            release=self.release,
            identifier="RXNORM:1",
            label="Example medicine",
            synonyms=["Sample synonym"],
        )
        self.unclassified = Concept.objects.create(
            release=self.release, identifier="RXNORM:2", label="Unclassified medicine"
        )
        EntityTranslation.objects.create(
            concept=self.drug,
            language="zh-Hans",
            label="示例药物",
            provider="test",
            status="reference",
            source_version="v1",
            match_basis="test",
            content_sha256="c" * 64,
        )
        self.dataset = ResourceDataset.objects.create(
            namespace="RXCLASS_EPC",
            version="epc-v1",
            manifest_sha256="b" * 64,
            manifest={"state": "completed"},
            counts={"entities": 2, "assertions": 3},
            active=True,
        )
        for identifier, label in [("N001", "Class Alpha"), ("N002", "Class Beta")]:
            ResourceEntity.objects.create(
                dataset=self.dataset, kind="class", identifier=identifier, label=label
            )
            self.membership(identifier, self.drug.identifier)
        self.membership("N001", "RXNORM:99")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vocabulary = Path(self.temp.name) / "classes.json"
        self.vocabulary.write_text(
            json.dumps(
                {
                    "version": "test-zh-v1",
                    "source": "https://example.org/classification",
                    "labels": {
                        "N001": {
                            "en": "Class Alpha",
                            "zh": "甲类药物",
                            "match_basis": "project translation",
                        },
                        # An old English identity must not silently label a different class.
                        "N002": {"en": "Former class", "zh": "不应采用旧译名"},
                    },
                }
            )
        )
        self.patch = patch(
            "apps.curation.resource_labels.TRANSLATION_FILE", self.vocabulary
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def membership(self, class_id, drug_id, dataset=None):
        return ResourceAssertion.objects.create(
            dataset=dataset or self.dataset,
            kind="class_membership",
            source_record_id=f"{drug_id}:{class_id}",
            subject_identifier=drug_id,
            object_identifier=class_id,
            predicate="has_epc",
        )

    def get(self, **params):
        response = drug_tree(self.factory.get("/drug-tree/", params))
        return response.status_code, json.loads(response.content)

    def test_classes_and_multi_class_membership_do_not_create_false_parents(self):
        status, result = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(
            result["coverage"],
            {"classes": 2, "classified_ingredients": 1, "local_ingredients": 2},
        )
        self.assertEqual([r["child_count"] for r in result["results"]], [1, 1])
        self.assertEqual(result["results"][0]["unavailable_count"], 1)
        for class_id in ("N001", "N002"):
            status, children = self.get(class_id=class_id)
            self.assertEqual(status, 200)
            self.assertEqual(
                [r["identifier"] for r in children["results"]], ["RXNORM:1"]
            )
            self.assertEqual(
                children["results"][0]["translations"][0]["label"], "示例药物"
            )
        self.assertNotIn("parents", result["results"][0])

    def test_chinese_class_and_ingredient_search(self):
        _, result = self.get(q="甲类")
        self.assertEqual([r["identifier"] for r in result["results"]], ["N001"])
        _, children = self.get(q="甲类", class_id="N001")
        self.assertEqual(children["total"], 1)
        for query in ("示例药物", "Sample synonym", "RXNORM:1"):
            _, result = self.get(q=query)
            self.assertEqual(result["total"], 2)
            _, children = self.get(q=query, class_id="N001")
            self.assertEqual(children["total"], 1)
        _, result = self.get(q="Unclassified medicine")
        self.assertEqual(result["total"], 0)

    def test_class_translation_requires_exact_source_label(self):
        _, result = self.get()
        self.assertEqual(
            result["results"][0]["translations"][0]["status"], "project_draft"
        )
        self.assertEqual(result["results"][1]["translations"], [])
        _, result = self.get(q="不应采用旧译名")
        self.assertEqual(result["total"], 0)

    def test_release_pin_uses_only_that_rxnorm_identity(self):
        other = SourceRelease.objects.create(
            namespace="RXNORM", version="rx-v2", sha256="d" * 64, count=1
        )
        Concept.objects.create(
            release=other, identifier="RXNORM:99", label="Newly available"
        )
        _, result = self.get(class_id="N001", release=str(other.pk))
        self.assertEqual(result["release"]["id"], str(other.pk))
        self.assertEqual([r["identifier"] for r in result["results"]], ["RXNORM:99"])
        self.assertEqual(self.get(release="invalid")[0], 400)

    def test_dataset_pin_keeps_previous_completed_version(self):
        self.dataset.active = False
        self.dataset.save(update_fields=["active"])
        new = ResourceDataset.objects.create(
            namespace="RXCLASS_EPC",
            version="epc-v2",
            manifest_sha256="d" * 64,
            manifest={"state": "completed"},
            counts={"entities": 1, "assertions": 1},
            active=True,
        )
        ResourceEntity.objects.create(
            dataset=new, kind="class", identifier="N003", label="New class"
        )
        self.membership("N003", "RXNORM:2", new)
        _, current = self.get()
        self.assertEqual(current["dataset"]["id"], str(new.pk))
        _, pinned = self.get(dataset=str(self.dataset.pk), class_id="N001")
        self.assertEqual(pinned["dataset"]["version"], "epc-v1")
        self.assertEqual(pinned["total"], 1)
        self.assertEqual(self.get(class_id="N001")[0], 400)

    def test_staged_snapshot_is_not_exposed_as_complete(self):
        self.dataset.active = False
        self.dataset.counts = {}
        self.dataset.save(update_fields=["active", "counts"])
        _, result = self.get()
        self.assertEqual(result["state"], "waiting_dataset")
        self.assertEqual(result["results"], [])
        self.assertEqual(self.get(dataset=str(self.dataset.pk))[0], 400)

    def test_branch_and_class_pagination_have_stable_distinct_rows(self):
        for index in range(3, 65):
            identifier = f"RXNORM:{index}"
            Concept.objects.create(
                release=self.release, identifier=identifier, label=f"Drug {index:03}"
            )
            self.membership("N001", identifier)
            ResourceEntity.objects.create(
                dataset=self.dataset,
                kind="class",
                identifier=f"N{index:03}",
                label=f"Class {index:03}",
            )
        for params, expected in [({"class_id": "N001"}, 63), ({}, 64)]:
            _, first = self.get(**params, limit=50)
            _, second = self.get(**params, offset=50, limit=50)
            ids = [row["identifier"] for row in first["results"] + second["results"]]
            self.assertEqual(first["total"], expected)
            self.assertEqual(len(ids), expected)
            self.assertEqual(len(set(ids)), expected)

    def test_invalid_class_and_query_are_rejected(self):
        self.assertEqual(self.get(class_id="unknown")[0], 400)
        self.assertEqual(self.get(q="x" * 201)[0], 400)
        self.assertEqual(self.get(dataset="invalid")[0], 400)
