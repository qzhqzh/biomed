import copy

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from apps.curation import entity_registry as registry
from apps.curation.entity_registry_models import (
    CanonicalEntity,
    EntityRegistryDecision,
    EntityRegistryImport,
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.models import (
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    Material,
    MaterialVersion,
    Passage,
)


class EntityRegistryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "entity-reviewer", password="fixture-only"
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="change_evidence")
        )
        self.payload = {
            "schema_version": registry.SCHEMA,
            "batch_id": "seed-one",
            "source": {"kind": "bootstrap", "snapshot_sha256": "a" * 64},
            "entities": [
                {
                    "key": "drug:a",
                    "kind": "drug",
                    "name": "Drug A",
                    "status": "active",
                    "source_status": "seed",
                },
                {
                    "key": "disease:glioma",
                    "kind": "disease",
                    "name": "脑胶质瘤",
                    "level": "main",
                    "status": "active",
                    "source_status": "seed",
                },
                {
                    "key": "disease:dmg",
                    "kind": "disease",
                    "name": "弥漫性中线胶质瘤",
                    "level": "subtype",
                    "main_key": "disease:glioma",
                    "status": "active",
                    "source_status": "seed",
                },
            ],
            "rules": [
                self.rule("Drug A", "drug:a"),
                self.rule("DMG", "disease:dmg", kind="disease"),
            ],
        }

    def rule(self, alias, key=None, kind="drug", **kwargs):
        return {
            "kind": kind,
            "alias": alias,
            "canonical_key": key,
            "status": "confirmed" if key else "unresolved",
            "action": "alias",
            "origin": "dictionary",
            "reason": "Fixture explicit dictionary",
            **kwargs,
        }

    def mention(self, index, label="Drug A", kind="drug"):
        material = Material.objects.create(
            source_key=f"registry:{index}",
            kind="paper",
            title=f"Article {index}",
            identifiers={"pubmed": str(index)},
        )
        version = MaterialVersion.objects.create(
            material=material,
            sha256=str(index).zfill(64),
            media_type="application/xml",
            filename="fixture.xml",
            rights="fixture",
        )
        passage = Passage.objects.create(
            version=version,
            parser_version="test",
            ordinal=1,
            locator="body/p:1",
            text=label + " treatment",
        )
        fact = {
            "entity_roles": {"e1": ["intervention" if kind == "drug" else "disease"]},
            "population": "H3 K27-altered at midline",
            "conditions": ["adult"],
            "original": label,
        }
        candidate = KnowledgeCandidate.objects.create(
            source_run="registry-test",
            group_id=f"K{index:02}",
            version=version,
            content_sha256="a" * 64,
            import_sha256="b" * 64,
            fact=fact,
        )
        entity = KnowledgeEntity.objects.create(
            identity_key=registry.digest([index, kind]),
            scope_version=version,
            kind=kind,
            label=label,
        )
        KnowledgeAnchor.objects.create(
            candidate=candidate,
            entity=entity,
            passage=passage,
            content_sha256="c" * 64,
            entity_ref="e1",
            purpose="mention",
            association="context_only",
            start=0,
            end=len(label),
            quote=label,
        )
        return candidate, {
            "source_run": candidate.source_run,
            "group_id": candidate.group_id,
            "entity_ref": "e1",
            "qualifiers": {"site": "midline", "molecular": "H3 K27-altered"},
        }

    def load_mentions(self, mentions, rules=None):
        return registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "literature-one",
                "source": {"kind": "literature"},
                "mentions": mentions,
                "rules": rules or [],
            }
        )

    def test_bootstrap_idempotent_sealed_and_dry_run(self):
        result = registry.import_registry(self.payload, dry_run=True)
        self.assertEqual(result["created_entities"], 3)
        self.assertEqual(CanonicalEntity.objects.count(), 0)
        registry.import_registry(self.payload)
        self.assertTrue(registry.import_registry(self.payload)["reused"])
        altered = copy.deepcopy(self.payload)
        altered["entities"][0]["name"] = "Different"
        with self.assertRaises(registry.RegistryConflict):
            registry.import_registry(altered)
        altered["batch_id"] = "seed-two"
        with self.assertRaisesMessage(registry.RegistryConflict, "封存"):
            registry.import_registry(altered)
        self.assertEqual(EntityRegistryImport.objects.count(), 1)

    def test_cross_article_reuse_preserves_originals_and_context(self):
        registry.import_registry(self.payload)
        c1, m1 = self.mention(1)
        _, m2 = self.mention(2)
        before = (c1.fact, c1.publication_status)
        self.load_mentions([m1, m2])
        mentions = list(EntityRegistryMention.objects.all())
        self.assertEqual(len({m.canonical_entity_id for m in mentions}), 1)
        self.assertEqual({m.state for m in mentions}, {"resolved"})
        self.assertNotEqual(mentions[0].entity_id, mentions[1].entity_id)
        self.assertEqual(mentions[0].qualifiers["molecular"], "H3 K27-altered")
        c1.refresh_from_db()
        self.assertEqual((c1.fact, c1.publication_status), before)
        again = self.load_mentions([m1, m2])
        self.assertTrue(again["reused"])
        self.assertEqual(EntityRegistryMention.objects.count(), 2)

    def test_scoped_ambiguity_blocks_global_but_same_name_other_type_is_separate(self):
        registry.import_registry(self.payload)
        c1, m1 = self.mention(1)
        _, m2 = self.mention(2)
        _, m3 = self.mention(3, kind="disease")
        scoped = self.rule(
            "Drug A", scope={"type": "article", "key": str(c1.version_id)}
        )
        self.load_mentions(
            [m1, m2, m3], [scoped, self.rule("Drug A", "disease:dmg", kind="disease")]
        )
        self.assertEqual(
            EntityRegistryMention.objects.get(candidate=c1).state, "unresolved"
        )
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 2
        )
        scoped_rule = EntityResolutionRule.objects.get(scope_type="article")
        registry.review_rule(
            scoped_rule.pk,
            {
                "expected_revision": 1,
                "action": "confirm",
                "canonical_id": str(CanonicalEntity.objects.get(key="drug:a").pk),
                "reason": "Explicit article definition",
            },
            self.user,
        )
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 3
        )

    def test_review_revoke_restore_and_revision_audit(self):
        registry.import_registry(self.payload)
        _, m = self.mention(1)
        self.load_mentions([m])
        rule = EntityResolutionRule.objects.get(alias="Drug A")
        registry.review_rule(
            rule.pk,
            {
                "expected_revision": 1,
                "action": "revoke",
                "reason": "Withdraw mistaken alias",
            },
            self.user,
        )
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")
        with self.assertRaises(registry.RegistryConflict):
            registry.review_rule(
                rule.pk,
                {
                    "expected_revision": 1,
                    "action": "confirm",
                    "reason": "Stale browser",
                },
                self.user,
            )
        registry.review_rule(
            rule.pk,
            {
                "expected_revision": 2,
                "action": "confirm",
                "reason": "Checked full source",
            },
            self.user,
        )
        self.assertEqual(EntityRegistryMention.objects.get().state, "resolved")
        events = EntityRegistryDecision.objects.filter(subject_id=rule.pk)
        self.assertEqual(events.count(), 3)
        self.assertEqual(events.last().actor_id, self.user.pk)
        with self.assertRaises(ValueError):
            events.update(reason="overwrite")

    def test_wrong_type_and_single_component_regimen_merge_rejected(self):
        bad = copy.deepcopy(self.payload)
        bad["rules"][0]["canonical_key"] = "disease:dmg"
        with self.assertRaisesMessage(ValueError, "跨实体类型"):
            registry.import_registry(bad)
        bad = copy.deepcopy(self.payload)
        bad["entities"][0].update(
            name="Drug A + Drug B",
            provenance={
                "source_form": "possible_combination",
                "source_aliases": ["A+B"],
            },
        )
        with self.assertRaisesMessage(ValueError, "组合药"):
            registry.import_registry(bad)
        self.assertEqual(CanonicalEntity.objects.count(), 0)

    def test_hierarchy_protects_main_and_retains_subtype(self):
        registry.import_registry(self.payload)
        main = CanonicalEntity.objects.get(key="disease:glioma")
        subtype = CanonicalEntity.objects.get(key="disease:dmg")
        self.assertEqual(subtype.main_cancer_id, main.pk)
        with self.assertRaisesMessage(ValueError, "仍有亚型"):
            registry.review_entity(
                main.pk,
                {
                    "expected_revision": 1,
                    "action": "set_classification",
                    "level": "unassigned",
                    "reason": "Invalid parent demotion",
                },
                self.user,
            )
        with self.assertRaisesMessage(ValueError, "亚型必须"):
            registry.review_entity(
                subtype.pk,
                {
                    "expected_revision": 1,
                    "action": "set_classification",
                    "level": "subtype",
                    "main_id": str(subtype.pk),
                    "reason": "Invalid cycle",
                },
                self.user,
            )
        main.refresh_from_db()
        self.assertEqual(main.revision, 1)

    def test_new_entity_creation_and_alias_reused(self):
        registry.import_registry(self.payload)
        _, m1 = self.mention(1, label="New Drug")
        _, m2 = self.mention(2, label="New Drug")
        self.load_mentions(
            [m1, m2],
            [self.rule("New Drug", origin="model", reason="Suggest novel entity")],
        )
        rule = EntityResolutionRule.objects.get(alias="New Drug")
        reviewed, changes = registry.review_rule(
            rule.pk,
            {
                "expected_revision": 1,
                "action": "create",
                "name": "New Drug",
                "reason": "Confirmed independent drug",
            },
            self.user,
        )
        self.assertEqual(changes["changed"], 2)
        self.assertEqual(reviewed.canonical_entity.source_status, "literature_new")
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 2
        )

    def test_proposals_do_not_resolve_and_background_rejected(self):
        self.payload["rules"][0].update(status="proposed", origin="model")
        registry.import_registry(self.payload)
        candidate, mention = self.mention(1)
        self.load_mentions([mention])
        self.assertEqual(EntityRegistryMention.objects.get().state, "proposed")
        self.assertIsNone(EntityRegistryMention.objects.get().canonical_entity_id)
        candidate.fact["entity_roles"] = {"e1": ["comparator"]}
        candidate.save(update_fields=["fact"])
        with self.assertRaisesMessage(ValueError, "背景或对照"):
            registry.import_registry(
                {
                    "schema_version": registry.SCHEMA,
                    "batch_id": "other",
                    "source": {"kind": "literature"},
                    "mentions": [mention],
                }
            )

    def test_system_classification_is_bounded_versioned_and_not_human(self):
        self.payload["entities"].append(
            {
                "key": "disease:new",
                "kind": "disease",
                "name": "Unassigned subtype",
                "status": "active",
                "source_status": "seed",
            }
        )
        registry.import_registry(self.payload)
        payload = {
            "schema_version": registry.SCHEMA,
            "batch_id": "classify-one",
            "source": {"kind": "literature"},
            "classifications": [
                {
                    "entity_key": "disease:new",
                    "level": "subtype",
                    "main_key": "disease:glioma",
                    "expected_revision": 1,
                    "origin": "model",
                    "reason": "Independent reference reviewed",
                    "evidence": [{"quote": "Explicit subtype"}],
                }
            ],
        }
        result = registry.import_registry(payload)
        self.assertEqual(result["classified_entities"], 1)
        entity = CanonicalEntity.objects.get(key="disease:new")
        event = EntityRegistryDecision.objects.filter(subject_id=entity.pk).last()
        self.assertEqual(event.origin, "model")
        self.assertIsNone(event.actor)
        self.assertEqual(entity.provenance["classification_origin"], "model")
        payload["batch_id"] = "overwrite-classification"
        payload["classifications"][0].update(
            level="main", main_key=None, expected_revision=2
        )
        with self.assertRaisesMessage(registry.RegistryConflict, "既有主癌种"):
            registry.import_registry(payload)
        registry.review_entity(
            entity.pk,
            {
                "expected_revision": 2,
                "action": "set_classification",
                "level": "unassigned",
                "reason": "User leaves uncertain",
            },
            self.user,
        )
        payload["classifications"][0].update(
            level="subtype", main_key="disease:glioma", expected_revision=3
        )
        with self.assertRaisesMessage(registry.RegistryConflict, "人工审核"):
            registry.import_registry(payload)

    def test_qualified_mapping_retains_conditions_and_withdraws_only_derived_fields(
        self,
    ):
        registry.import_registry(self.payload)
        candidate, mention = self.mention(1, label="adult DMG", kind="disease")
        original = copy.deepcopy(candidate.fact)
        rule = self.rule(
            "adult DMG",
            "disease:dmg",
            kind="disease",
            origin="model",
            provenance={
                "match_relation": "qualified_form",
                "required_qualifiers": ["adult"],
            },
        )
        self.load_mentions([mention], [rule])
        registered = EntityRegistryMention.objects.get()
        self.assertEqual(
            registered.qualifiers["rule_qualifiers"]["required_qualifiers"], ["adult"]
        )
        self.assertEqual(registered.qualifiers["molecular"], "H3 K27-altered")
        repeated = {
            "schema_version": registry.SCHEMA,
            "batch_id": "mention-again",
            "source": {"kind": "literature"},
            "mentions": [mention],
        }
        self.assertEqual(registry.import_registry(repeated)["created_mentions"], 0)
        saved_rule = EntityResolutionRule.objects.get(alias="adult DMG")
        registry.review_rule(
            saved_rule.pk,
            {"expected_revision": 1, "action": "revoke", "reason": "Withdraw mapping"},
            self.user,
        )
        registered.refresh_from_db()
        self.assertNotIn("rule_qualifiers", registered.qualifiers)
        self.assertEqual(registered.qualifiers["molecular"], "H3 K27-altered")
        candidate.refresh_from_db()
        self.assertEqual(candidate.fact, original)

    def test_qualified_complete_name_reuses_across_articles_but_abbreviation_stays_scoped(
        self,
    ):
        from scripts.build_entity_registry_reuse import can_reuse_qualified_rule

        registry.import_registry(self.payload)
        c1, first = self.mention(
            1, label="adult diffuse midline glioma", kind="disease"
        )
        _, second = self.mention(
            2, label="adult diffuse midline glioma", kind="disease"
        )
        rule = self.rule(
            "adult diffuse midline glioma",
            "disease:dmg",
            kind="disease",
            origin="model",
            scope={"type": "article", "key": str(c1.version_id)},
            provenance={
                "match_relation": "qualified_form",
                "required_qualifiers": ["adult"],
            },
        )
        self.load_mentions([first, second], [rule])
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 1
        )
        self.assertTrue(can_reuse_qualified_rule(rule))
        global_rule = copy.deepcopy(rule)
        global_rule["scope"] = {"type": "global", "key": ""}
        registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "qualified-extension",
                "source": {"kind": "literature"},
                "rules": [global_rule],
            }
        )
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 2
        )
        for mention in EntityRegistryMention.objects.all():
            self.assertEqual(
                mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["adult"]
            )
        self.assertFalse(can_reuse_qualified_rule({**rule, "alias": "DMG"}))
        self.assertFalse(can_reuse_qualified_rule({**rule, "alias": "adult DMG"}))
        self.assertFalse(
            can_reuse_qualified_rule(
                {
                    **rule,
                    "provenance": {
                        "match_relation": "qualified_form",
                        "required_qualifiers": ["adult", "recurrent"],
                    },
                }
            )
        )
        self.assertFalse(can_reuse_qualified_rule(rule, [global_rule]))

    def test_punctuation_placeholder_cannot_be_confirmed_and_is_quarantined_once(self):
        self.payload["rules"].append(self.rule("-"))
        self.payload["rules"].append(self.rule("NA"))
        registry.import_registry(self.payload)
        rule = EntityResolutionRule.objects.get(alias="-")
        with self.assertRaisesMessage(ValueError, "纯标点"):
            registry.review_rule(
                rule.pk,
                {
                    "expected_revision": 1,
                    "action": "confirm",
                    "canonical_id": str(CanonicalEntity.objects.get(key="drug:a").pk),
                    "reason": "Cannot identify a placeholder",
                },
                self.user,
            )
        result = registry.quarantine_placeholder_rules()
        self.assertEqual(result["rejected"], 1)
        rule.refresh_from_db()
        self.assertEqual(rule.status, "rejected")
        self.assertEqual(rule.revision, 2)
        event = EntityRegistryDecision.objects.filter(subject_id=rule.pk).last()
        self.assertEqual(event.action, "quarantine_placeholder")
        self.assertIsNone(event.actor)
        self.assertEqual(event.before["status"], "unresolved")
        self.assertEqual(
            EntityResolutionRule.objects.get(alias="NA").status, "unresolved"
        )
        self.assertEqual(registry.quarantine_placeholder_rules()["rejected"], 0)
        self.assertEqual(CanonicalEntity.objects.count(), 3)

    def test_system_rule_update_and_candidate_activation_require_revision_hash_and_no_human(
        self,
    ):
        self.payload["entities"][0]["status"] = "candidate"
        self.payload["rules"][0]["status"] = "proposed"
        registry.import_registry(self.payload)
        entity = CanonicalEntity.objects.get(key="drug:a")
        rule = EntityResolutionRule.objects.get(alias="Drug A")
        update = {
            **self.payload["rules"][0],
            "status": "confirmed",
            "origin": "rule",
            "reason": "Verified class identity",
            "expected_revision": 1,
            "expected_content_sha256": registry.digest(registry.rule_snapshot(rule)),
        }
        activation = {
            "entity_key": entity.key,
            "expected_revision": 1,
            "expected_content_sha256": registry.digest(
                registry.entity_snapshot(entity)
            ),
            "origin": "rule",
            "reason": "Verified class identity",
            "provenance_patch": {"entity_level": "drug_class"},
        }
        payload = {
            "schema_version": registry.SCHEMA,
            "batch_id": "system-update-v2",
            "source": {"kind": "literature"},
            "rule_updates": [update],
            "new_candidates_activation": [activation],
        }
        wrong = copy.deepcopy(payload)
        wrong["rule_updates"][0]["expected_content_sha256"] = "0" * 64
        with self.assertRaisesMessage(registry.RegistryConflict, "内容哈希"):
            registry.import_registry(wrong)
        entity.refresh_from_db()
        self.assertEqual(entity.status, "candidate")
        result = registry.import_registry(payload)
        self.assertEqual(
            (result["updated_rules"], result["activated_entities"]), (1, 1)
        )
        self.assertTrue(registry.import_registry(payload)["reused"])
        entity.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual(entity.provenance["entity_level"], "drug_class")
        self.assertEqual(rule.status, "confirmed")
        self.assertEqual(rule.revision, 2)
        event = EntityRegistryDecision.objects.filter(subject_id=rule.pk).last()
        self.assertEqual(event.action, "system_update")
        self.assertIsNone(event.actor)
        registry.review_rule(
            rule.pk,
            {
                "expected_revision": 2,
                "action": "defer",
                "reason": "Human leaves uncertain",
            },
            self.user,
        )
        rule.refresh_from_db()
        payload.update(batch_id="system-after-human", new_candidates_activation=[])
        update.update(
            expected_revision=3,
            expected_content_sha256=registry.digest(registry.rule_snapshot(rule)),
        )
        with self.assertRaisesMessage(registry.RegistryConflict, "人工审核"):
            registry.import_registry(payload)

    def test_browse_and_review_auth_permission_csrf(self):
        registry.import_registry(self.payload)
        _, mention = self.mention(1)
        self.load_mentions([mention], [self.rule("Unused")])
        prefix = "/api/v1/curation/entity-registry/"
        self.assertFalse(self.client.get(prefix + "summary/").json()["can_review"])
        drugs = self.client.get(prefix + "entities/?kind=drug").json()
        self.assertEqual(drugs["count"], 1)
        self.assertEqual(drugs["results"][0]["mention_count"], 1)
        detail = self.client.get(
            prefix + "entities/" + drugs["results"][0]["id"] + "/"
        ).json()
        self.assertEqual(detail["mentions"][0]["anchors"][0]["quote"], "Drug A")
        self.assertEqual(
            detail["mentions"][0]["context"]["population"], "H3 K27-altered at midline"
        )
        self.assertEqual(
            self.client.get(prefix + "rules/?status=pending&has_mentions=true").json()[
                "count"
            ],
            0,
        )
        self.assertEqual(
            self.client.get(prefix + "rules/?status=pending&has_mentions=false").json()[
                "count"
            ],
            1,
        )
        rule = EntityResolutionRule.objects.get(alias="Drug A")
        url = prefix + f"rules/{rule.pk}/review/"
        body = {"expected_revision": 1, "action": "revoke", "reason": "Review testing"}
        self.assertEqual(
            self.client.post(url, body, content_type="application/json").status_code,
            401,
        )
        plain_user = get_user_model().objects.create_user("reader")
        self.client.force_login(plain_user)
        self.assertEqual(
            self.client.post(url, body, content_type="application/json").status_code,
            403,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(url, body, content_type="application/json").status_code, 403
        )
        client.get("/api/v1/curation/session/")
        token = client.cookies["csrftoken"].value
        answer = client.post(
            url, body, content_type="application/json", HTTP_X_CSRFTOKEN=token
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json()["changed_mentions"], 1)
        self.assertEqual(
            client.post(
                url, body, content_type="application/json", HTTP_X_CSRFTOKEN=token
            ).status_code,
            409,
        )
