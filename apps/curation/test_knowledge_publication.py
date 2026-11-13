import copy

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from apps.curation.knowledge_publication import (
    apply_review_batch,
    build_review_packet,
    human_review,
)
from apps.curation.knowledge_storage import digest, import_candidates
from apps.curation.materials import quarantine_version
from apps.curation.models import (
    Evidence,
    KnowledgeAnchor,
    KnowledgeCandidate,
    KnowledgeEntity,
    KnowledgeReview,
    KnowledgeSearchTerm,
    Material,
    MaterialVersion,
    Passage,
)


class KnowledgePublicationTests(TestCase):
    def setUp(self):
        material = Material.objects.create(
            source_key="publication:test",
            kind="paper",
            title="Fixture",
            identifiers={"pubmed": "123"},
        )
        self.version = MaterialVersion.objects.create(
            material=material,
            sha256="a" * 64,
            media_type="application/xml",
            filename="test.xml",
            rights="fixture",
        )
        self.text = "BTC patients received durvalumab. CRP groups had different PFS. ICI is background."
        self.passage = Passage.objects.create(
            version=self.version,
            parser_version="test-v1",
            ordinal=1,
            locator="body/p:1",
            text=self.text,
        )
        self.entities = [
            ("disease", "BTC"),
            ("drug", "durvalumab"),
            ("biomarker", "CRP"),
            ("treatment", "ICI"),
        ]
        fact = {
            "population": "BTC patients",
            "drug_names": ["durvalumab"],
            "treatment": "durvalumab",
            "results": [{"measure": "PFS", "value": "different"}],
        }
        record = {
            "group_id": "K01",
            "version_id": str(self.version.pk),
            "source_sha256": self.version.sha256,
            "article": {"material_id": str(material.pk), "pmid": "123"},
            "fact": fact,
            "assessment": {"old_research": "pass"},
            "provenance": {"test": "immutable"},
            "entities": [
                {"ref": f"K01.e{i}", "kind": kind, "label": term}
                for i, (kind, term) in enumerate(self.entities, 1)
            ],
            "anchors": [
                {
                    "entity_ref": f"K01.e{i}",
                    **self.quote(term),
                    "purpose": "mention",
                    "association": "context_only",
                    "roles": [],
                }
                for i, (_, term) in enumerate(self.entities, 1)
            ],
        }
        import_candidates(
            {
                "schema_version": "knowledge-candidates-v1",
                "source_run": "publication-test",
                "provenance": {},
                "records": [record],
            }
        )
        self.candidate = KnowledgeCandidate.objects.get()
        self.packet = build_review_packet(self.candidate)
        self.payload = {
            "review_run": "actual-independent-test",
            "provider": "fixture",
            "model": "fixture-reviewer",
            "policy_version": "bindings-v1",
            "records": [
                {
                    "candidate_id": str(self.candidate.pk),
                    "input_sha256": digest(self.packet),
                    "decision": "pass",
                    "reason": "Fixture independent review",
                    "extra_passage_ids": [],
                    "approved_links": [
                        {
                            "entity_ref": f"K01.e{i}",
                            "role": kind,
                            "aliases": [
                                {"term": "胆道癌", "basis": "project_translation"}
                            ]
                            if i == 1
                            else [],
                            "support": [self.quote(term)],
                        }
                        for i, (kind, term) in enumerate(self.entities[:3], 1)
                    ],
                    "supporting_quotes": [
                        {
                            **self.quote(self.text),
                            "fields": [
                                "population",
                                "drug_names",
                                "treatment",
                                "results",
                            ],
                        }
                    ],
                }
            ],
        }
        self.user = get_user_model().objects.create_user(
            "knowledge-reviewer", password="fixture-only-password"
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="change_evidence")
        )

    def quote(self, term):
        start = self.text.index(term)
        return {
            "passage_id": str(self.passage.pk),
            "start": start,
            "end": start + len(term),
            "quote": term,
        }

    def test_extracted_relationship_filters_do_not_need_review_or_use_context(self):
        url = "/api/v1/curation/knowledge-candidates/"
        filters = {"disease": "BTC", "drug": "durvalumab", "biomarker": "CRP"}
        # Merely having matching types and mentions must not establish a relation.
        self.assertEqual(self.client.get(url, filters).json()["count"], 0)
        for index, role in enumerate(("disease", "intervention", "molecular"), 1):
            self.candidate.anchors.filter(entity_ref=f"K01.e{index}").update(
                association="model_selected", roles=[role]
            )
        self.candidate.assessment = {
            "execution": {"status": "complete"},
            "compatibility": {"status": "pending_ids", "missing": ["target_uuid"]},
            "quality": {"status": "unreviewed"},
        }
        self.candidate.save(update_fields=["assessment"])
        result = self.client.get(url, filters).json()
        self.assertEqual(result["count"], 1)
        row = result["results"][0]
        self.assertEqual(row["execution"]["status"], "complete")
        self.assertEqual(row["compatibility"]["status"], "pending_ids")
        self.assertEqual(row["quality"]["status"], "unreviewed")
        self.assertEqual(row["ai_status"], "pending")
        self.assertEqual(row["publication_status"], "draft")
        self.assertEqual(len(row["relationship_participants"]), 3)
        self.assertEqual(self.client.get(url, {**filters, "drug": "ICI"}).json()["count"], 0)
        self.candidate.anchors.filter(entity_ref="K01.e2").update(roles=["comparator"])
        self.assertEqual(self.client.get(url, filters).json()["count"], 0)
        # A drug relationship in another fact from the same article cannot fill
        # the missing intervention in this fact.
        other = KnowledgeCandidate.objects.create(
            source_run="publication-test", group_id="K02", version=self.version,
            content_sha256="b" * 64, import_sha256="c" * 64,
        )
        original = self.candidate.anchors.get(entity_ref="K01.e2")
        KnowledgeAnchor.objects.create(
            candidate=other, entity=original.entity, entity_ref="K02.e1",
            passage=self.passage, content_sha256="d" * 64,
            start=original.start, end=original.end, quote=original.quote,
            purpose="mention", association="model_selected", roles=["intervention"],
        )
        self.assertEqual(self.client.get(url, {"disease": "BTC", "drug": "durvalumab"}).json()["count"], 0)
        self.assertEqual(self.client.get(url, {"drug": "x" * 2001}).status_code, 400)
        # The existing published endpoint keeps its release contract.
        self.assertEqual(self.client.get("/api/v1/curation/knowledge/").json()["count"], 0)

    def test_quality_only_review_is_append_only_and_does_not_publish(self):
        result = apply_review_batch(self.payload, publish=False)
        self.assertEqual(result["mode"], "quality_only")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["published"], 0)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.ai_status, "pending")
        self.assertEqual(self.candidate.publication_status, "draft")
        self.assertIsNone(self.candidate.current_review_id)
        self.assertFalse(KnowledgeSearchTerm.objects.exists())
        review = KnowledgeReview.objects.get()
        self.assertEqual(review.kind, "quality")
        self.assertEqual(apply_review_batch(self.payload, publish=False)["reused"], 1)
        self.assertEqual(KnowledgeReview.objects.count(), 1)
        detail = self.client.get(f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/").json()
        self.assertEqual(detail["quality"]["status"], "valid")
        self.assertEqual(detail["quality"]["review_id"], str(review.pk))
        self.assertEqual(detail["execution"]["status"], "stored")
        self.assertEqual(detail["fact"], self.candidate.fact)

    def test_quality_failure_does_not_withdraw_or_replace_publication_review(self):
        apply_review_batch(self.payload)
        self.candidate.refresh_from_db()
        publication_review = self.candidate.current_review_id
        terms = list(KnowledgeSearchTerm.objects.values_list("id", flat=True))
        failed = copy.deepcopy(self.payload)
        failed["records"][0].update(decision="fail", reason="Independent quality finding", approved_links=[])
        result = apply_review_batch(failed, publish=False)
        self.assertEqual(result["created"], 1)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.publication_status, "published")
        self.assertEqual(self.candidate.ai_status, "pass")
        self.assertEqual(self.candidate.current_review_id, publication_review)
        self.assertEqual(list(KnowledgeSearchTerm.objects.values_list("id", flat=True)), terms)
        detail = self.client.get(f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/").json()
        self.assertEqual(detail["quality"]["status"], "invalid")
        changed = copy.deepcopy(failed)
        changed["records"][0]["reason"] = "Changed existing quality review"
        with self.assertRaisesRegex(ValueError, "different content"):
            apply_review_batch(changed, publish=False)

    def test_undetermined_quality_does_not_mean_failed_extraction_or_publish(self):
        pending = copy.deepcopy(self.payload)
        pending["records"][0].update(
            decision="undetermined", reason="Insufficient source context",
            approved_links=[], supporting_quotes=[],
        )
        dry = apply_review_batch(pending, publish=False, dry_run=True)
        self.assertEqual(dry["decisions"]["undetermined"], 1)
        self.assertFalse(KnowledgeReview.objects.exists())
        apply_review_batch(pending, publish=False)
        detail = self.client.get(f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/").json()
        self.assertEqual(detail["quality"]["status"], "undetermined")
        self.assertEqual(detail["execution"]["status"], "stored")
        self.assertEqual(detail["publication_status"], "draft")
        with self.assertRaises(ValueError):
            apply_review_batch(pending)

    def target_quality_payload(self):
        payload = copy.deepcopy(self.payload)
        payload["schema_version"] = "knowledge-quality-v1"
        payload["policy_version"] = "target-core-quality-v1"
        record = payload["records"][0]
        record.pop("approved_links")
        # This source quote supports the core relationship. It deliberately
        # does not bind old results/other optional fields or approve entity IDs.
        record["supporting_quotes"] = [
            {**self.quote("BTC patients received durvalumab."), "fields": ["treatment"]}
        ]
        record["quality_dimensions"] = {
            "core_valid": True, "target_covered": False, "participants_usable": False,
        }
        record["issues"] = [{"field": "entity_roles", "detail": "Target participant coverage remains partial"}]
        return payload

    def test_target_quality_accepts_core_only_quotes_without_publication_bindings(self):
        payload = self.target_quality_payload()
        original_fact = copy.deepcopy(self.candidate.fact)
        result = apply_review_batch(payload, publish=False)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["published"], 0)
        self.assertEqual(apply_review_batch(payload, publish=False)["reused"], 1)
        self.assertFalse(KnowledgeSearchTerm.objects.exists())
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.fact, original_fact)
        self.assertEqual(self.candidate.ai_status, "pending")
        self.assertEqual(self.candidate.publication_status, "draft")
        self.assertIsNone(self.candidate.current_review_id)
        review = KnowledgeReview.objects.get()
        self.assertNotIn("approved_links", review.result)
        self.assertEqual(review.result["schema_version"], "knowledge-quality-v1")
        row = self.client.get(f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/").json()
        self.assertEqual(row["quality"]["status"], "valid")
        self.assertEqual(row["quality"]["quality_dimensions"], payload["records"][0]["quality_dimensions"])
        self.assertEqual(row["quality"]["issues"], payload["records"][0]["issues"])
        changed = copy.deepcopy(payload)
        changed["records"][0]["quality_dimensions"]["target_covered"] = True
        with self.assertRaisesRegex(ValueError, "different content"):
            apply_review_batch(changed, publish=False)

    def test_target_quality_cannot_be_used_for_publication_or_weaken_legacy_mode(self):
        payload = self.target_quality_payload()
        with self.assertRaisesRegex(ValueError, "only supported in quality-only mode"):
            apply_review_batch(payload)
        del payload["schema_version"]
        with self.assertRaisesRegex(ValueError, "approved relationship links"):
            apply_review_batch(payload, publish=False)
        self.assertFalse(KnowledgeReview.objects.exists())

    def test_target_quality_still_checks_input_identity_and_exact_quotes(self):
        for change in ("hash", "quote", "passage", "missing_quote"):
            payload = self.target_quality_payload()
            record = payload["records"][0]
            if change == "hash":
                record["input_sha256"] = "f" * 64
            elif change == "quote":
                record["supporting_quotes"][0]["quote"] = "Invented source statement"
            elif change == "passage":
                record["supporting_quotes"][0]["passage_id"] = "missing-source"
            else:
                record["decision"] = "undetermined"
                record["supporting_quotes"] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_review_batch(payload, publish=False)
            self.assertFalse(KnowledgeReview.objects.exists())

    def test_target_quality_requires_explicit_boolean_dimensions(self):
        for dimensions in (
            {},
            {"core_valid": 1, "target_covered": True, "participants_usable": True},
            {"core_valid": None, "target_covered": True, "participants_usable": True},
        ):
            payload = self.target_quality_payload()
            payload["records"][0]["quality_dimensions"] = dimensions
            with self.subTest(dimensions=dimensions), self.assertRaisesRegex(ValueError, "quality_dimensions"):
                apply_review_batch(payload, publish=False)
        payload = self.target_quality_payload()
        payload["records"][0]["issues"] = [False]
        with self.assertRaisesRegex(ValueError, "issues must"):
            apply_review_batch(payload, publish=False)
        self.assertFalse(KnowledgeReview.objects.exists())

    def test_old_research_assessment_never_publishes_and_packet_excludes_it(self):
        self.assertNotIn("assessment", self.packet["candidate"])
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/").json()["count"], 0
        )
        old = self.client.get(
            f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/"
        ).json()
        self.assertEqual(old["status"], "research_candidate")
        self.assertEqual(old["assessment"], {"old_research": "pass"})
        self.assertEqual(old["publication_status"], "draft")
        missing = copy.deepcopy(self.payload)
        missing["model"] = ""
        with self.assertRaises(ValueError):
            apply_review_batch(missing)

    def test_publish_search_and_provenance(self):
        self.payload["records"][0]["approved_links"][1]["context"] = "Current treatment"
        apply_review_batch(self.payload)
        response = self.client.get(
            "/api/v1/curation/knowledge/",
            {"disease": "胆道癌", "drug": " DURVALUMAB ", "biomarker": "crp"},
        ).json()
        self.assertEqual(response["count"], 1)
        item = response["results"][0]
        self.assertEqual(len(item["matched_terms"]), 3)
        self.assertEqual(item["fact"], self.candidate.fact)
        self.assertEqual(item["article"]["pmid"], "123")
        self.assertEqual(item["approved_links"][1]["context"], "Current treatment")
        quote = item["supporting_quotes"][0]
        self.assertEqual(
            quote["quote"], quote["passage"]["text"][quote["start"] : quote["end"]]
        )
        self.assertEqual(quote["passage"]["parser_version"], "test-v1")
        self.assertEqual(Evidence.objects.count(), 0)
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/", {"drug": "durva"}).json()[
                "count"
            ],
            0,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/knowledge/", {"disease": "胆道癌", "treatment": "ICI"}
            ).json()["count"],
            0,
        )
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/", {"q": "ICI"}).json()[
                "count"
            ],
            0,
        )

    def test_ungraded_context_is_available_in_candidate_and_published_search_without_changing_release(self):
        self.text += " First-line therapy was used."
        self.passage.text = self.text
        self.passage.save(update_fields=["text"])
        self.candidate.fact = {
            **self.candidate.fact, "conditions": ["first-line therapy"], "evidence": ["P1"],
            "results": [{**self.candidate.fact["results"][0], "passage": "P1"}],
        }
        self.candidate.save(update_fields=["fact"])
        KnowledgeAnchor.objects.create(
            candidate=self.candidate, passage=self.passage, content_sha256="f" * 64,
            purpose="evidence", association="context_only", start=0, end=len(self.text),
            quote=self.text, attributes={"alias": "P1"},
        )
        url = f"/api/v1/curation/knowledge-candidates/{self.candidate.pk}/"
        candidate = self.client.get(url).json()
        presentation = candidate["evidence_presentation"]
        self.assertEqual(presentation["evidence_level"]["status"], "unknown")
        self.assertEqual(presentation["drug_effect"]["status"], "unknown")
        self.assertEqual(presentation["reported_context"]["requirements"][0]["reported_label"], "一线治疗")
        self.assertEqual(candidate["publication_status"], "draft")
        self.assertEqual(self.client.get("/api/v1/curation/knowledge/").json()["count"], 0)

        packet = build_review_packet(self.candidate)
        self.payload["records"][0]["input_sha256"] = digest(packet)
        self.payload["records"][0]["supporting_quotes"] = [{
            **self.quote(self.text), "fields": ["population", "drug_names", "treatment", "results", "conditions"],
        }]
        apply_review_batch(self.payload)
        published = self.client.get("/api/v1/curation/knowledge/", {"drug": "durvalumab"}).json()
        self.assertEqual(published["count"], 1)
        displayed = published["results"][0]["evidence_presentation"]
        self.assertEqual(displayed["evidence_level"]["label"], "未分级")
        self.assertEqual(displayed["drug_effect"]["label"], "未判定")
        self.assertEqual(displayed["reported_context"]["requirements"][0]["reported_label"], "一线治疗")
        self.assertFalse(displayed["reported_context"]["automatic_patient_eligibility"])

    def test_fail_review_stays_out_of_published_search(self):
        self.payload["records"][0].update(
            decision="fail", approved_links=[], supporting_quotes=[]
        )
        apply_review_batch(self.payload)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.ai_status, "fail")
        self.assertEqual(self.candidate.publication_status, "draft")
        self.assertEqual(KnowledgeSearchTerm.objects.count(), 0)
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/").json()["count"], 0
        )

    def test_dry_run_replay_and_conflicting_review_do_not_overwrite(self):
        apply_review_batch(self.payload, dry_run=True)
        self.assertEqual(KnowledgeReview.objects.count(), 0)
        apply_review_batch(self.payload)
        result = apply_review_batch(self.payload)
        self.assertEqual(result["reused"], 1)
        self.assertEqual(KnowledgeReview.objects.count(), 1)
        conflict = copy.deepcopy(self.payload)
        conflict["records"][0]["reason"] = "Changed"
        with self.assertRaisesRegex(ValueError, "different content"):
            apply_review_batch(conflict)
        review = KnowledgeReview.objects.get()
        review.reason = "Attempt mutation"
        with self.assertRaisesRegex(ValueError, "immutable"):
            review.save()
        with self.assertRaisesRegex(ValueError, "immutable"):
            KnowledgeReview.objects.update(reason="Attempt mutation")

    def test_changed_input_hash_and_changed_source_are_rejected(self):
        self.candidate.fact["population"] = "Changed"
        self.candidate.save(update_fields=["fact"])
        with self.assertRaisesRegex(ValueError, "input_sha256 mismatch"):
            apply_review_batch(self.payload)
        self.assertEqual(KnowledgeReview.objects.count(), 0)
        self.passage.text = "Changed source"
        self.passage.save(update_fields=["text"])
        with self.assertRaisesRegex(ValueError, "exactly matches"):
            apply_review_batch(self.payload)

    def test_exact_quotes_bindings_and_explicit_drug_are_required(self):
        for change, message in [
            (
                lambda r: r["supporting_quotes"][0].update(quote="rewritten source"),
                "exactly match",
            ),
            (
                lambda r: r["supporting_quotes"][0].update(fields=["population"]),
                "lacks source bindings",
            ),
            (lambda r: r["approved_links"].pop(1), "explicit drug_name"),
        ]:
            payload = copy.deepcopy(self.payload)
            change(payload["records"][0])
            with self.assertRaisesRegex(ValueError, message):
                apply_review_batch(payload)
        self.assertEqual(KnowledgeReview.objects.count(), 0)

    def test_additional_same_version_definition_is_hashed_and_traceable(self):
        definition = Passage.objects.create(
            version=self.version,
            parser_version="test-v1",
            ordinal=2,
            locator="abstract/p:1",
            text="biliary tract cancer (BTC)",
        )
        packet = build_review_packet(self.candidate, [definition.pk])
        self.assertNotEqual(digest(packet), digest(self.packet))
        raw = self.payload["records"][0]
        raw["input_sha256"] = digest(packet)
        raw["extra_passage_ids"] = [str(definition.pk)]
        raw["approved_links"][0]["support"].append(
            {
                "passage_id": str(definition.pk),
                "start": 0,
                "end": len(definition.text),
                "quote": definition.text,
            }
        )
        apply_review_batch(self.payload)
        result = self.client.get(
            f"/api/v1/curation/knowledge/{self.candidate.pk}/"
        ).json()
        self.assertEqual(
            result["approved_links"][0]["support"][1]["passage"]["locator"],
            "abstract/p:1",
        )

    def test_cross_version_extra_passage_is_rejected(self):
        version = MaterialVersion.objects.create(
            material=self.version.material,
            sha256="b" * 64,
            filename="other.xml",
            media_type="application/xml",
        )
        passage = Passage.objects.create(
            version=version,
            parser_version="test",
            ordinal=1,
            locator="other",
            text="BTC",
        )
        with self.assertRaisesRegex(ValueError, "different source version"):
            build_review_packet(self.candidate, [passage.pk])

    def test_review_can_select_existing_category_over_same_named_wrong_drug(self):
        mistaken = KnowledgeEntity.objects.create(
            identity_key="c" * 64, scope_version=self.version, kind="drug", label="ICI"
        )
        KnowledgeAnchor.objects.create(
            candidate=self.candidate,
            entity=mistaken,
            entity_ref="K01.e5",
            content_sha256="d" * 64,
            purpose="mention",
            association="context_only",
            **self.quote("ICI"),
        )
        self.candidate.fact["drug_names"] = ["ICI"]
        self.candidate.save(update_fields=["fact"])
        raw = self.payload["records"][0]
        raw["input_sha256"] = digest(build_review_packet(self.candidate))
        raw["approved_links"].append(
            {
                "entity_ref": "K01.e4",
                "role": "treatment",
                "aliases": [],
                "support": [self.quote("ICI")],
                "context": "Treatment class, not a specific drug",
            }
        )
        apply_review_batch(self.payload)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.publication_status, "published")
        self.assertFalse(KnowledgeSearchTerm.objects.filter(entity=mistaken).exists())

    def test_category_alias_cannot_replace_specific_drug(self):
        raw = self.payload["records"][0]
        raw["approved_links"].pop(1)
        raw["approved_links"].append(
            {
                "entity_ref": "K01.e4",
                "role": "treatment",
                "aliases": [
                    {"term": "durvalumab", "basis": "incorrect category substitution"}
                ],
                "support": [self.quote("ICI")],
            }
        )
        with self.assertRaisesRegex(ValueError, "specific drug cannot"):
            apply_review_batch(self.payload)

    def test_source_quarantine_withdraws_and_default_query_filters_direct_quarantine(
        self,
    ):
        apply_review_batch(self.payload)
        MaterialVersion.objects.filter(pk=self.version.pk).update(
            integrity_status="quarantined"
        )
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/").json()["count"], 0
        )
        MaterialVersion.objects.filter(pk=self.version.pk).update(
            integrity_status="valid"
        )
        quarantine_version(self.version.pk, "test source concern")
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.publication_status, "withdrawn")
        with self.assertRaisesRegex(ValueError, "quarantined"):
            apply_review_batch(self.payload)

    def test_human_flag_is_idempotent_and_ai_cannot_resurrect(self):
        apply_review_batch(self.payload)
        human_review(self.candidate.pk, self.user, "flag", "Source concern")
        human_review(self.candidate.pk, self.user, "flag", "Source concern")
        self.assertEqual(KnowledgeReview.objects.filter(kind="human").count(), 1)
        apply_review_batch(self.payload)
        fresh = copy.deepcopy(self.payload)
        fresh["review_run"] = "second-ai-review"
        apply_review_batch(fresh)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.publication_status, "withdrawn")
        self.assertEqual(self.candidate.human_status, "flagged")
        self.assertEqual(
            self.client.get("/api/v1/curation/knowledge/").json()["count"], 0
        )
        history = self.client.get(
            f"/api/v1/curation/knowledge/{self.candidate.pk}/"
        ).json()["review_history"]
        self.assertEqual(history[1]["actor"], self.user.username)

    def test_human_review_requires_identity_permission_reason_and_csrf(self):
        apply_review_batch(self.payload)
        url = f"/api/v1/curation/knowledge/{self.candidate.pk}/review/"
        data = {"decision": "confirm", "reason": "Checked original source"}
        self.assertEqual(
            self.client.post(url, data, content_type="application/json").status_code,
            401,
        )
        powerless = get_user_model().objects.create_user("reader")
        self.client.force_login(powerless)
        self.assertEqual(
            self.client.post(url, data, content_type="application/json").status_code,
            403,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(url, data, content_type="application/json").status_code, 403
        )
        client.get("/api/v1/curation/session/")
        token = client.cookies["csrftoken"].value
        for invalid in [
            {"decision": "pass", "reason": "No browser AI pass"},
            {"decision": "confirm", "reason": ""},
        ]:
            self.assertEqual(
                client.post(
                    url,
                    invalid,
                    content_type="application/json",
                    HTTP_X_CSRFTOKEN=token,
                ).status_code,
                400,
            )
        self.assertEqual(
            client.post(
                url, data, content_type="application/json", HTTP_X_CSRFTOKEN=token
            ).status_code,
            200,
        )
        review = KnowledgeReview.objects.get(kind="human")
        self.assertEqual(review.actor, self.user)

    def test_failed_new_review_withdraws_and_old_terms_stop_matching(self):
        apply_review_batch(self.payload)
        fail = copy.deepcopy(self.payload)
        fail["review_run"] = "new-negative-review"
        fail["records"][0].update(
            decision="fail",
            reason="Mismatch identified",
            approved_links=[],
            supporting_quotes=[],
        )
        apply_review_batch(fail)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.publication_status, "withdrawn")
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/knowledge/", {"drug": "durvalumab"}
            ).json()["count"],
            0,
        )
