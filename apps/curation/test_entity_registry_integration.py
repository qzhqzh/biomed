import copy
import json
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.curation import entity_registry as registry
from apps.curation.entity_registry_integration import register_candidates
from apps.curation.entity_registry_models import (
    EntityRegistryMention,
    EntityResolutionRule,
)
from apps.curation.knowledge_models import (
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeReview,
    KnowledgeSearchTerm,
)
from apps.curation.knowledge_storage import import_candidates
from apps.curation.models import Material, MaterialVersion, Passage


class RegistryIntegrationTests(TestCase):
    def setUp(self):
        material = Material.objects.create(
            source_key="integration-fixture", kind="paper", title="Fixture"
        )
        self.version = MaterialVersion.objects.create(
            material=material,
            sha256="a" * 64,
            media_type="application/xml",
            filename="fixture.xml",
            rights="fixture",
        )
        self.passage = Passage.objects.create(
            version=self.version,
            parser_version="test",
            ordinal=1,
            locator="body/p:1",
            text="Drug A Drug B DMG HER2+ breast cancer breast cancer",
        )
        registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "registry-integration-seed",
                "source": {"kind": "bootstrap"},
                "entities": [
                    {
                        "key": "drug:a",
                        "kind": "drug",
                        "name": "药甲",
                        "name_en": "Drug A",
                        "status": "active",
                    },
                    {
                        "key": "drug:b",
                        "kind": "drug",
                        "name": "药乙",
                        "name_en": "Drug B",
                        "status": "active",
                    },
                    {
                        "key": "disease:main",
                        "kind": "disease",
                        "name": "脑胶质瘤",
                        "name_en": "Glioma",
                        "level": "main",
                        "status": "active",
                    },
                    {
                        "key": "disease:dmg",
                        "kind": "disease",
                        "name": "弥漫性中线胶质瘤",
                        "level": "subtype",
                        "main_key": "disease:main",
                        "status": "active",
                    },
                    {
                        "key": "disease:breast",
                        "kind": "disease",
                        "name": "乳腺癌",
                        "name_en": "breast cancer",
                        "level": "main",
                        "status": "active",
                    },
                ],
                "rules": [
                    self.rule("Drug A", "drug:a"),
                    self.rule("Drug A synonym", "drug:a"),
                    self.rule("Glioma synonym", "disease:main", kind="disease"),
                    self.rule("Drug B", "drug:b"),
                    self.rule("DMG", "disease:dmg", kind="disease"),
                    self.rule("breast cancer", "disease:breast", kind="disease"),
                    self.rule(
                        "HER2+ breast cancer",
                        "disease:breast",
                        kind="disease",
                        provenance={
                            "match_relation": "qualified_form",
                            "required_qualifiers": ["HER2+"],
                        },
                    ),
                ],
            }
        )

    def rule(self, alias, canonical, kind="drug", **extra):
        return {
            "kind": kind,
            "alias": alias,
            "canonical_key": canonical,
            "status": "confirmed",
            "action": "alias",
            "origin": "rule",
            "reason": "Synthetic explicit dictionary",
            **extra,
        }

    def record(self, group, participants):
        entities, anchors, roles = [], [], {}
        for index, (kind, label, role) in enumerate(participants):
            ref = f"{group}.e{index}"
            entities.append({"ref": ref, "kind": kind, "label": label})
            start = self.passage.text.index(label)
            anchors.append(
                {
                    "entity_ref": ref,
                    "passage_id": str(self.passage.pk),
                    "start": start,
                    "end": start + len(label),
                    "quote": label,
                    "purpose": "mention",
                    "association": "model_selected"
                    if role != "context"
                    else "context_only",
                    "roles": [role],
                }
            )
            roles[ref] = [role]
        return {
            "group_id": group,
            "version_id": str(self.version.pk),
            "source_sha256": self.version.sha256,
            "article": {"material_id": str(self.version.material_id)},
            "fact": {
                "entity_roles": roles,
                "population": "original population",
                "conditions": ["original condition"],
            },
            "entities": entities,
            "anchors": anchors,
        }

    def payload(self, records):
        return {
            "schema_version": "knowledge-candidates-v1",
            "source_run": "registry-intake-test",
            "records": records,
        }

    def publish(self, candidate, approved_refs):
        review = KnowledgeReview.objects.create(
            candidate=candidate,
            review_key=registry.digest(str(candidate.pk)),
            kind="ai",
            decision="pass",
            reason="Synthetic review fixture",
            provider="fixture",
            model="fixture",
            policy_version="fixture",
            review_run="fixture",
            input_sha256="b" * 64,
            input_snapshot={},
            result={},
        )
        for anchor in candidate.anchors.filter(
            entity_ref__in=approved_refs
        ).select_related("entity"):
            KnowledgeSearchTerm.objects.create(
                candidate=candidate,
                review=review,
                entity=anchor.entity,
                role=anchor.entity.kind,
                term=anchor.entity.label,
                normalized_term=registry.normalize(anchor.entity.label),
                basis="synthetic review",
            )
        candidate.current_review = review
        candidate.ai_status = "pass"
        candidate.publication_status = "published"
        candidate.save(
            update_fields=["current_review", "ai_status", "publication_status"]
        )

    def test_intake_and_replay_register_participants_without_rewriting_fact_or_context(
        self,
    ):
        payload = self.payload(
            [
                self.record(
                    "K1",
                    [
                        ("drug", "Drug A", "intervention"),
                        ("drug", "Drug B", "comparator"),
                        ("disease", "DMG", "disease"),
                    ],
                )
            ]
        )
        frozen = copy.deepcopy(payload)
        first = import_candidates(payload)
        self.assertEqual(first["entity_registry"]["created_mentions"], 2)
        self.assertEqual(
            EntityRegistryMention.objects.filter(state="resolved").count(), 2
        )
        candidate = KnowledgeCandidate.objects.get()
        self.assertEqual(candidate.fact, frozen["records"][0]["fact"])
        self.assertEqual(candidate.anchors.count(), 3)
        self.assertEqual(candidate.publication_status, "draft")
        self.assertFalse(candidate.reviews.exists())
        EntityRegistryMention.objects.filter(kind="drug").delete()
        second = import_candidates(payload)
        self.assertTrue(second["reused"])
        self.assertEqual(second["entity_registry"]["created_mentions"], 1)
        self.assertFalse(EntityRegistryMention.objects.filter(label="Drug B").exists())
        self.assertEqual(payload, frozen)

    def test_candidate_search_uses_canonical_names_main_cancer_and_same_fact_role_and(
        self,
    ):
        import_candidates(
            self.payload(
                [
                    self.record(
                        "K1",
                        [
                            ("drug", "Drug A", "intervention"),
                            ("drug", "Drug B", "comparator"),
                        ],
                    ),
                    self.record("K2", [("disease", "DMG", "disease")]),
                    self.record(
                        "K3",
                        [
                            ("drug", "Drug A", "intervention"),
                            ("disease", "DMG", "disease"),
                            ("drug", "Drug B", "context"),
                        ],
                    ),
                ]
            )
        )
        url = "/api/v1/curation/knowledge-candidates/"
        data = self.client.get(url, {"disease": "脑胶质瘤", "drug": "药甲"}).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["group_id"], "K3")
        normalized = data["results"][0]["normalized_participants"]
        self.assertEqual(len(normalized), 2)
        self.assertEqual({p["state"] for p in normalized}, {"resolved"})
        disease = next(p for p in normalized if p["canonical"]["kind"] == "disease")
        self.assertEqual(disease["canonical"]["main_cancer"]["name"], "脑胶质瘤")
        self.assertEqual(disease["original_label"], "DMG")
        self.assertEqual(disease["applied_rule"]["status"], "confirmed")
        self.assertEqual(
            data["results"][0]["eligibility"]["semantic_correctness"], "unassessed"
        )
        self.assertEqual(
            self.client.get(
                url, {"drug": "Drug A synonym", "disease": "Glioma synonym"}
            ).json()["count"],
            1,
        )
        self.assertEqual(
            self.client.get(url, {"disease": "脑胶质瘤", "drug": "药乙"}).json()[
                "count"
            ],
            0,
        )
        self.assertEqual(
            self.client.get(url, {"disease": "DMG", "drug": "Drug A"}).json()["count"],
            1,
        )
        self.assertEqual(
            self.client.get(url, {"disease": "弥漫性中线胶质瘤"}).json()["count"], 2
        )
        self.assertEqual(self.client.get(url, {"disease": "Glioma"}).json()["count"], 2)
        self.assertEqual(self.client.get(url, {"q": "脑胶质瘤"}).json()["count"], 2)

    def test_qualified_alias_search_cannot_expand_to_unqualified_parent(self):
        import_candidates(
            self.payload(
                [
                    self.record("K1", [("disease", "HER2+ breast cancer", "disease")]),
                    self.record("K2", [("disease", "breast cancer", "disease")]),
                ]
            )
        )
        url = "/api/v1/curation/knowledge-candidates/"
        self.assertEqual(self.client.get(url, {"disease": "乳腺癌"}).json()["count"], 2)
        self.assertEqual(
            self.client.get(url, {"disease": "HER2+ breast cancer"}).json()["count"], 1
        )
        for candidate in KnowledgeCandidate.objects.all():
            self.publish(candidate, list(candidate.fact["entity_roles"]))
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/knowledge/", {"disease": "HER2+ breast cancer"}
            ).json()["count"],
            1,
        )

    def test_published_canonical_search_requires_same_current_approved_entity_role(
        self,
    ):
        import_candidates(
            self.payload(
                [
                    self.record(
                        "K1",
                        [
                            ("drug", "Drug A", "intervention"),
                            ("disease", "DMG", "disease"),
                        ],
                    ),
                    self.record(
                        "K2",
                        [
                            ("drug", "Drug A", "intervention"),
                            ("disease", "DMG", "disease"),
                        ],
                    ),
                ]
            )
        )
        first, second = list(KnowledgeCandidate.objects.order_by("group_id"))
        self.publish(first, ["K1.e0"])
        self.publish(second, ["K2.e1"])
        url = "/api/v1/curation/knowledge/"
        self.assertEqual(self.client.get(url, {"drug": "药甲"}).json()["count"], 1)
        self.assertEqual(
            self.client.get(url, {"drug": "Drug A synonym"}).json()["count"], 1
        )
        self.assertEqual(
            self.client.get(url, {"disease": "脑胶质瘤"}).json()["count"], 1
        )
        self.assertEqual(
            self.client.get(url, {"drug": "药甲", "disease": "脑胶质瘤"}).json()[
                "count"
            ],
            0,
        )
        self.assertEqual(self.client.get(url, {"q": "脑胶质瘤"}).json()["count"], 1)
        self.assertEqual(self.client.get(url, {"drug": "药"}).json()["count"], 0)

    def test_command_registration_does_not_override_existing_qualifiers_or_rules(self):
        payload = self.payload(
            [self.record("K1", [("drug", "Drug A", "intervention")])]
        )
        import_candidates(payload)
        mention = EntityRegistryMention.objects.get()
        mention.qualifiers = {"existing_experiment": {"source": "preserved"}}
        mention.save(update_fields=["qualifiers"])
        result = register_candidates([mention.candidate_id], chunk_size=1)
        self.assertEqual(result["created_mentions"], 0)
        mention.refresh_from_db()
        self.assertEqual(
            mention.qualifiers, {"existing_experiment": {"source": "preserved"}}
        )

    def test_legacy_current_review_registers_only_approved_participants_and_replay_searches_canonical(
        self,
    ):
        record = self.record(
            "K1",
            [
                ("drug", "Drug A", "context"),
                ("disease", "DMG", "context"),
                ("drug", "Drug B", "context"),
            ],
        )
        record["fact"].pop("entity_roles")
        payload = self.payload([record])
        before = copy.deepcopy(payload)
        self.assertEqual(
            import_candidates(payload)["entity_registry"]["created_mentions"], 0
        )
        candidate = KnowledgeCandidate.objects.get()
        self.publish(candidate, ["K1.e0", "K1.e1"])
        result = import_candidates(payload)
        self.assertTrue(result["reused"])
        self.assertEqual(result["entity_registry"]["created_mentions"], 2)
        self.assertEqual(
            set(EntityRegistryMention.objects.values_list("label", flat=True)),
            {"Drug A", "DMG"},
        )
        self.assertEqual(
            {
                m.qualifiers["participation_basis"]
                for m in EntityRegistryMention.objects.all()
            },
            {"current_approved_review"},
        )
        candidate.refresh_from_db()
        self.assertEqual(candidate.fact, before["records"][0]["fact"])
        self.assertEqual(candidate.reviews.count(), 1)
        self.assertTrue(
            all(anchor.roles == ["context"] for anchor in candidate.anchors.all())
        )
        response = self.client.get(
            "/api/v1/curation/knowledge/", {"drug": "药甲", "disease": "脑胶质瘤"}
        ).json()
        self.assertEqual(response["count"], 1)
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/", {"drug": "药乙"}).json()[
                "count"
            ],
            0,
        )

    def test_legacy_relationship_anchors_exclude_comparator_and_context(self):
        record = self.record(
            "K1",
            [
                ("drug", "Drug A", "intervention"),
                ("disease", "DMG", "disease"),
                ("drug", "Drug B", "comparator"),
            ],
        )
        record["fact"].pop("entity_roles")
        result = import_candidates(self.payload([record]))
        self.assertEqual(result["entity_registry"]["created_mentions"], 2)
        self.assertEqual(
            {
                m.qualifiers["participation_basis"]
                for m in EntityRegistryMention.objects.all()
            },
            {"relationship_anchor"},
        )
        self.assertFalse(EntityRegistryMention.objects.filter(label="Drug B").exists())

    def test_legacy_obsolete_review_and_explicit_context_roles_do_not_fall_back(self):
        record = self.record("K1", [("drug", "Drug A", "context")])
        record["fact"].pop("entity_roles")
        import_candidates(self.payload([record]))
        candidate = KnowledgeCandidate.objects.get()
        self.publish(candidate, ["K1.e0"])
        old_review = candidate.current_review
        current = KnowledgeReview.objects.create(
            candidate=candidate,
            review_key="new-current-review",
            kind="ai",
            decision="fail",
            reason="Synthetic current rejection",
            provider="fixture",
            model="fixture",
            policy_version="fixture",
            review_run="replacement",
            input_sha256="c" * 64,
            input_snapshot={},
            result={},
        )
        candidate.current_review = current
        candidate.save(update_fields=["current_review"])
        self.assertEqual(register_candidates([candidate.pk])["created_mentions"], 0)

        # Explicit context-only roles remain authoritative even with approved terms.
        candidate.current_review = old_review
        candidate.fact["entity_roles"] = {"K1.e0": ["context"]}
        candidate.save(update_fields=["current_review", "fact"])
        self.assertEqual(register_candidates([candidate.pk])["created_mentions"], 0)

    def test_keyword_search_does_not_multiply_count_and_matching_anchor_joins(self):
        payload = self.payload(
            [
                self.record(
                    "K1",
                    [("drug", "Drug A", "intervention"), ("disease", "DMG", "disease")],
                )
            ]
        )
        import_candidates(payload)
        candidate = KnowledgeCandidate.objects.get()
        original = candidate.anchors.first()
        KnowledgeAnchor.objects.bulk_create(
            [
                KnowledgeAnchor(
                    candidate=candidate,
                    entity=original.entity,
                    passage=self.passage,
                    content_sha256=registry.digest(["many-anchors", i]),
                    entity_ref=original.entity_ref,
                    purpose="context",
                    association="context_only",
                    roles=[],
                    start=original.start,
                    end=original.end,
                    quote=original.quote,
                )
                for i in range(40)
            ]
        )
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                "/api/v1/curation/knowledge-candidates/",
                {"q": "Drug A", "source_run": payload["source_run"]},
            ).json()
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["results"][0]["anchor_count"], 42)
        self.assertEqual(response["results"][0]["entity_count"], 2)
        candidate_queries = [
            entry["sql"]
            for entry in captured
            if 'FROM "curation_knowledgecandidate"' in entry["sql"]
        ]
        self.assertTrue(candidate_queries)
        self.assertTrue(
            all(
                sql.count('LEFT OUTER JOIN "curation_knowledgeanchor"') <= 1
                for sql in candidate_queries
            )
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/knowledge-candidates/",
                {"q": "Drug A", "source_run": "other-run"},
            ).json()["count"],
            0,
        )

    def test_parenthetical_name_requires_both_confirmed_forms_and_preserves_raw_fact(
        self,
    ):
        self.passage.text += " diffuse midline glioma (DMG)"
        self.passage.save(update_fields=["text"])
        registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "long-form",
                "source": {"kind": "literature"},
                "rules": [
                    self.rule("diffuse midline glioma", "disease:dmg", kind="disease")
                ],
            }
        )
        payload = self.payload(
            [
                self.record(
                    "K1", [("disease", "diffuse midline glioma (DMG)", "disease")]
                )
            ]
        )
        before = copy.deepcopy(payload)
        result = import_candidates(payload)
        self.assertEqual(result["entity_registry"]["derived_parenthetical_rules"], 1)
        mention = EntityRegistryMention.objects.get()
        self.assertEqual(mention.state, "resolved")
        self.assertEqual(mention.applied_rule.scope_type, "article")
        self.assertEqual(len(mention.applied_rule.provenance["source_rules"]), 2)
        self.assertEqual(
            mention.applied_rule.provenance["parenthetical_forms"],
            ["diffuse midline glioma", "DMG"],
        )
        self.assertEqual(mention.label, "diffuse midline glioma (DMG)")
        self.assertEqual(mention.candidate.fact, before["records"][0]["fact"])
        self.assertEqual(
            import_candidates(payload)["entity_registry"][
                "derived_parenthetical_rules"
            ],
            0,
        )

    def test_unknown_or_scope_conflicting_parenthetical_parts_stay_unresolved(self):
        self.passage.text += " DMG (unverified condition) Drug A (Drug B)"
        self.passage.save(update_fields=["text"])
        payload = self.payload(
            [
                self.record(
                    "K1",
                    [
                        ("disease", "DMG (unverified condition)", "disease"),
                        ("drug", "Drug A (Drug B)", "intervention"),
                    ],
                )
            ]
        )
        result = import_candidates(payload)
        self.assertEqual(result["entity_registry"]["derived_parenthetical_rules"], 0)
        self.assertEqual(
            set(EntityRegistryMention.objects.values_list("state", flat=True)),
            {"unresolved"},
        )
        registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "scope-conflict",
                "source": {"kind": "literature"},
                "rules": [
                    self.rule(
                        "Drug B",
                        "drug:a",
                        scope={"type": "article", "key": str(self.version.pk)},
                    )
                ],
            }
        )
        result = register_candidates(
            list(KnowledgeCandidate.objects.values_list("pk", flat=True))
        )
        self.assertEqual(result["derived_parenthetical_rules"], 0)
        self.assertEqual(
            set(EntityRegistryMention.objects.values_list("state", flat=True)),
            {"unresolved"},
        )

    def test_parenthetical_derivation_keeps_required_disease_qualifiers(self):
        self.passage.text += " HER2+ breast cancer (breast cancer)"
        self.passage.save(update_fields=["text"])
        result = import_candidates(
            self.payload(
                [
                    self.record(
                        "K1",
                        [("disease", "HER2+ breast cancer (breast cancer)", "disease")],
                    )
                ]
            )
        )
        self.assertEqual(result["entity_registry"]["derived_parenthetical_rules"], 1)
        mention = EntityRegistryMention.objects.get()
        self.assertEqual(
            mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["HER2+"]
        )
        self.assertEqual(
            mention.applied_rule.provenance["match_relation"], "qualified_form"
        )

    def seed_lung(self):
        registry.import_registry(
            {
                "schema_version": registry.SCHEMA,
                "batch_id": "qualified-lung-seed",
                "source": {"kind": "literature"},
                "entities": [{"key": "disease:lung", "kind": "disease", "name": "肺癌",
                              "name_en": "Non-small cell lung cancer", "level": "main", "status": "active"}],
                "rules": [self.rule("Non-small cell lung cancer", "disease:lung", kind="disease"),
                          self.rule("NSCLC", "disease:lung", kind="disease")],
            }
        )

    def seed_urothelial(self):
        registry.import_registry(
            {"schema_version": registry.SCHEMA, "batch_id": "qualified-urothelial-seed", "source": {"kind": "literature"},
             "entities": [{"key": "disease:urothelial", "kind": "disease", "name": "尿路上皮癌",
                           "name_en": "Urothelial carcinoma", "level": "main", "status": "active"}],
             "rules": [self.rule("Urothelial carcinoma", "disease:urothelial", kind="disease")]}
        )

    def with_evidence(self, record):
        record["anchors"].append({"entity_ref": None, "passage_id": str(self.passage.pk), "start": 0,
                                  "end": len(self.passage.text), "quote": self.passage.text,
                                  "purpose": "evidence", "association": "context_only", "roles": []})
        return record

    def test_qualified_names_and_unicode_hyphen_reuse_real_confirmed_identity(self):
        self.seed_lung()
        self.passage.text += " advanced non‐small cell lung cancer (NSCLC)"
        self.passage.save(update_fields=["text"])
        labels = ["advanced non‐small cell lung cancer", "advanced non‐small cell lung cancer (NSCLC)",
                  "non‐small cell lung cancer"]
        payload = self.payload([self.record(f"K{i}", [("disease", label, "disease")]) for i, label in enumerate(labels)])
        before = copy.deepcopy(payload)
        result = import_candidates(payload)
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 3)
        for mention in EntityRegistryMention.objects.order_by("candidate__group_id"):
            self.assertEqual(mention.state, "resolved")
            self.assertEqual(mention.canonical_entity.key, "disease:lung")
            self.assertEqual(mention.applied_rule.scope_type, "global")
            self.assertEqual(mention.applied_rule.origin, "rule")
            self.assertFalse(mention.applied_rule.provenance["human_confirmed"])
            self.assertEqual(mention.applied_rule.evidence[0]["quote"], mention.label)
            self.assertEqual(mention.applied_rule.evidence[0]["source_sha256"], self.version.sha256)
            if mention.label.startswith("advanced"):
                self.assertEqual(mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["advanced"])
            else:
                self.assertNotIn("rule_qualifiers", mention.qualifiers)
        for record in before["records"]:
            self.assertEqual(KnowledgeCandidate.objects.get(group_id=record["group_id"]).fact, record["fact"])
        self.assertEqual(import_candidates(payload)["entity_registry"]["derived_qualified_rules"], 0)

        other_material = Material.objects.create(source_key="other-qualified-fixture", kind="paper", title="Other")
        self.version = MaterialVersion.objects.create(material=other_material, sha256="b" * 64,
                                                      media_type="application/xml", filename="other.xml", rights="fixture")
        self.passage = Passage.objects.create(version=self.version, parser_version="test", ordinal=1,
                                              locator="body/p:1", text="advanced non‐small cell lung cancer")
        new_payload = self.payload([self.record("K-later", [("disease", labels[0], "disease")])])
        new_payload["source_run"] = "registry-second-article"
        reused = import_candidates(new_payload)
        self.assertEqual(reused["entity_registry"]["derived_qualified_rules"], 0)
        mention = EntityRegistryMention.objects.get(candidate__group_id="K-later")
        self.assertEqual(mention.state, "resolved")
        self.assertEqual(mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["advanced"])

    def test_register_command_replays_previously_unresolved_mentions(self):
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        payload = self.payload([self.record("K1", [("disease", label, "disease")])])
        self.assertEqual(import_candidates(payload)["entity_registry"]["states"], {"unresolved": 1})
        self.seed_lung()
        stream = StringIO()
        call_command("register_knowledge_entities", source_run=payload["source_run"], stdout=stream)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["derived_qualified_rules"], 1)
        self.assertFalse(result["target_contacted"])
        self.assertFalse(result["model_called"])
        mention = EntityRegistryMention.objects.get()
        self.assertEqual(mention.state, "resolved")
        self.assertEqual(mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["advanced"])

    def test_qualified_derivation_never_overrides_explicit_unresolved_full_name(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        pending = self.rule(label, None, kind="disease", status="unresolved", action="match")
        registry.import_registry({"schema_version": registry.SCHEMA, "batch_id": "existing-pending-full",
                                  "source": {"kind": "literature"}, "rules": [pending]})
        old_rule = EntityResolutionRule.objects.get(alias=label)
        old_snapshot = registry.rule_snapshot(old_rule)
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")
        old_rule.refresh_from_db()
        self.assertEqual(registry.rule_snapshot(old_rule), old_snapshot)

    def test_qualified_derivation_respects_conflicting_unicode_base_rule(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        pending = self.rule("non‐small cell lung cancer", None, kind="disease", status="unresolved", action="match",
                            scope={"type": "article", "key": str(self.version.pk)})
        registry.import_registry({"schema_version": registry.SCHEMA, "batch_id": "pending-variant-base",
                                  "source": {"kind": "literature"}, "rules": [pending]})
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")

    def test_conflicting_confirmed_hyphen_variants_are_not_merged(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        conflict = self.rule("non‐small cell lung cancer", "disease:breast", kind="disease",
                             scope={"type": "article", "key": str(self.version.pk)})
        registry.import_registry({"schema_version": registry.SCHEMA, "batch_id": "conflicting-variant-base",
                                  "source": {"kind": "literature"}, "rules": [conflict]})
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")

    def test_unresolved_other_article_prevents_global_alias_and_stays_unresolved(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        other_material = Material.objects.create(source_key="qualified-conflict-other", kind="paper", title="Other")
        other_version = MaterialVersion.objects.create(material=other_material, sha256="c" * 64,
                                                       media_type="application/xml", filename="other.xml", rights="fixture")
        pending = self.rule(label, None, kind="disease", status="unresolved", action="match",
                            scope={"type": "article", "key": str(other_version.pk)})
        registry.import_registry({"schema_version": registry.SCHEMA, "batch_id": "other-article-pending",
                                  "source": {"kind": "literature"}, "rules": [pending]})
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 1)
        current = EntityRegistryMention.objects.get(candidate__group_id="K1")
        self.assertEqual(current.state, "resolved")
        self.assertEqual(current.applied_rule.scope_type, "article")
        self.assertFalse(EntityResolutionRule.objects.filter(alias=label, scope_type="global").exists())
        self.version = other_version
        self.passage = Passage.objects.create(version=other_version, parser_version="test", ordinal=1,
                                              locator="body/p:1", text=label)
        other = self.payload([self.record("K-other", [("disease", label, "disease")])])
        other["source_run"] = "qualified-conflicting-article"
        import_candidates(other)
        self.assertEqual(EntityRegistryMention.objects.get(candidate__group_id="K-other").state, "unresolved")

    def test_parenthetical_components_must_name_same_confirmed_identity(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer (DMG)"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")

    def test_qualified_derivation_requires_exact_participating_source_anchor(self):
        self.seed_lung()
        label = "advanced non‐small cell lung cancer"
        self.passage.text += " " + label
        self.passage.save(update_fields=["text"])
        record = self.record("K1", [("disease", label, "disease")])
        record["anchors"][0].update(association="context_only", roles=["context"])
        result = import_candidates(self.payload([record]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get().state, "unresolved")

    def test_derived_advanced_name_keeps_existing_molecular_qualifier(self):
        self.passage.text += " advanced HER2+ breast cancer"
        self.passage.save(update_fields=["text"])
        label = "advanced HER2+ breast cancer"
        result = import_candidates(self.payload([self.record("K1", [("disease", label, "disease")])]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 1)
        mention = EntityRegistryMention.objects.get()
        self.assertEqual(mention.state, "resolved")
        self.assertEqual(mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["advanced", "HER2+"])

    def test_explicit_qualified_disease_and_abbreviation_are_scoped_and_preserved(self):
        self.seed_urothelial()
        full = "locally advanced or metastatic urothelial carcinoma"
        alias = "la/mUC"
        self.passage.text = f"Patients with {full} ({alias}) were studied."
        self.passage.save(update_fields=["text"])
        first = self.with_evidence(self.record("K1", [("disease", full, "disease"), ("disease", alias, "disease")]))
        second = self.with_evidence(self.record("K2", [("disease", alias, "disease")]))
        payload = self.payload([first, second])
        before = copy.deepcopy(payload)
        result = import_candidates(payload)
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 1)
        self.assertEqual(result["entity_registry"]["derived_article_abbreviation_rules"], 1)
        self.assertEqual(EntityRegistryMention.objects.filter(state="resolved").count(), 3)
        for mention in EntityRegistryMention.objects.all():
            self.assertEqual(mention.canonical_entity.key, "disease:urothelial")
            self.assertEqual(mention.qualifiers["rule_qualifiers"]["required_qualifiers"], ["locally advanced or metastatic"])
            self.assertEqual(mention.applied_rule.origin, "rule")
            if mention.label == alias:
                self.assertEqual(mention.applied_rule.scope_type, "article")
                self.assertEqual(mention.applied_rule.scope_key, str(self.version.pk))
                support = mention.applied_rule.evidence[0]
                self.assertEqual(self.passage.text[support["start"]:support["end"]], support["quote"])
        self.assertEqual(KnowledgeCandidate.objects.get(group_id="K1").fact, before["records"][0]["fact"])
        self.assertEqual(import_candidates(payload)["entity_registry"]["derived_article_abbreviation_rules"], 0)

        other_material = Material.objects.create(source_key="urothelial-other", kind="paper", title="Other")
        self.version = MaterialVersion.objects.create(material=other_material, sha256="d" * 64,
                                                      media_type="application/xml", filename="other.xml", rights="fixture")
        self.passage = Passage.objects.create(version=self.version, parser_version="test", ordinal=1,
                                              locator="body/p:1", text=f"Patients with {alias} were studied.")
        other = self.payload([self.with_evidence(self.record("K-other", [("disease", alias, "disease")]))])
        other["source_run"] = "other-urothelial-article"
        import_candidates(other)
        self.assertEqual(EntityRegistryMention.objects.get(candidate__group_id="K-other").state, "unresolved")

    def test_negated_qualified_disease_and_abbreviation_do_not_derive(self):
        self.seed_urothelial()
        full = "locally advanced or metastatic urothelial carcinoma"
        self.passage.text = f"Patients without {full} (la/mUC) were studied."
        self.passage.save(update_fields=["text"])
        record = self.with_evidence(self.record("K1", [("disease", full, "disease"), ("disease", "la/mUC", "disease")]))
        result = import_candidates(self.payload([record]))
        self.assertEqual(result["entity_registry"]["derived_qualified_rules"], 0)
        self.assertEqual(result["entity_registry"]["derived_article_abbreviation_rules"], 0)
        self.assertEqual(set(EntityRegistryMention.objects.values_list("state", flat=True)), {"unresolved"})

    def test_existing_scoped_unresolved_abbreviation_is_not_overridden(self):
        self.seed_urothelial()
        full, alias = "locally advanced or metastatic urothelial carcinoma", "la/mUC"
        self.passage.text = f"Patients with {full} ({alias}) were studied."
        self.passage.save(update_fields=["text"])
        pending = self.rule(alias, None, kind="disease", status="unresolved", action="match",
                            scope={"type": "article", "key": str(self.version.pk)})
        registry.import_registry({"schema_version": registry.SCHEMA, "batch_id": "scoped-abbr-pending",
                                  "source": {"kind": "literature"}, "rules": [pending]})
        record = self.with_evidence(self.record("K1", [("disease", full, "disease"), ("disease", alias, "disease")]))
        result = import_candidates(self.payload([record]))
        self.assertEqual(result["entity_registry"]["derived_article_abbreviation_rules"], 0)
        self.assertEqual(EntityRegistryMention.objects.get(label=alias).state, "unresolved")

    def test_scoped_abbreviation_can_use_an_exact_context_definition(self):
        self.seed_urothelial()
        full, alias = "locally advanced or metastatic urothelial carcinoma", "la/mUC"
        self.passage.text = f"Patients with {full} ({alias}) were studied."
        self.passage.save(update_fields=["text"])
        record = self.with_evidence(self.record("K1", [("disease", alias, "disease")]))
        record["anchors"][-1]["purpose"] = "context"
        result = import_candidates(self.payload([record]))
        self.assertEqual(result["entity_registry"]["derived_article_abbreviation_rules"], 1)
        mention = EntityRegistryMention.objects.get()
        self.assertEqual(mention.state, "resolved")
        self.assertEqual(mention.applied_rule.scope_type, "article")
