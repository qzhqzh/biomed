from django.test import TestCase

from apps.curation import test_entity_normalization as fixtures
from apps.curation.entity_normalization import normalize_bundle
from apps.curation.entity_registry import import_registry
from apps.curation.entity_registry_models import CanonicalEntity, EntityResolutionRule


class EntityNormalizationStorageTests(TestCase):
    def test_generated_identities_import_and_replay_in_the_existing_registry(self):
        registry = fixtures.registry()
        registry["entities"] = []
        patch, updated, report = normalize_bundle(fixtures.bundle("beta molecule"), registry, fixtures.index())
        self.assertEqual(report["counts"], {"resolved": 1})
        imported = import_registry(patch)
        self.assertEqual(imported["created_entities"], 1)
        entity = CanonicalEntity.objects.get()
        self.assertEqual(str(entity.pk), updated["entities"][0]["id"])
        rule = EntityResolutionRule.objects.get()
        self.assertEqual(str(rule.pk), updated["rules"][0]["id"])
        self.assertEqual(rule.status, "confirmed")
        self.assertEqual(rule.canonical_entity_id, entity.pk)
        self.assertTrue(import_registry(patch)["reused"])
        later = fixtures.bundle("beta molecule", version=fixtures.PASSAGE, source_run="future-batch")
        next_patch, _, report = normalize_bundle(later, updated, fixtures.index())
        self.assertEqual(report["counts"], {"reused": 1})
        self.assertEqual(import_registry(next_patch)["created_entities"], 0)
        self.assertEqual(CanonicalEntity.objects.count(), 1)

    def test_model_decision_can_register_without_a_human_approval_gate(self):
        registry = fixtures.registry()
        registry["entities"] = []
        patch, _, _ = normalize_bundle(fixtures.bundle("new named agent"), registry, fixtures.index(),
                                       decide=lambda item: fixtures.match(item, decision="NEW", name="new named agent"))
        import_registry(patch)
        self.assertEqual(CanonicalEntity.objects.get().status, "active")
        self.assertEqual(EntityResolutionRule.objects.get().origin, "model")
        self.assertEqual(EntityResolutionRule.objects.get().status, "confirmed")
