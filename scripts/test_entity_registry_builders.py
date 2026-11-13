"""Synthetic builder invariants; no model inference or fabricated study results."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_entity_registry_literature import build
from scripts.build_entity_registry_pilot import seed_payload
from scripts.build_entity_registry_reuse import build as build_reuse


class RegistryBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = {
            "bootstrap-v2.json": {"entities": [], "rules": []},
            "manifest.json": {"source_records": 2, "participating_mentions": 2},
            "comparison.json": {
                "selection": {"model": "synthetic-arm"},
                "arms": {"synthetic-arm": {"records": []}},
            },
            "reference.json": {"records": []},
            "candidate-catalog.json": {"records": []},
            "rules.json": {"records": []},
            "items.json": {"records": []},
        }

    def row(
        self,
        name,
        label,
        *,
        kind="drug",
        group="K1",
        candidates=None,
        cohort="unresolved_after_rules",
    ):
        return {
            "id": name,
            "label": label,
            "kind": kind,
            "cohort": cohort,
            "candidates": candidates or [],
            "mentions": [
                {
                    "source_run": "synthetic-fixture",
                    "group_id": group,
                    "entity_ref": name,
                    "version_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "source_sha256": "a" * 64,
                    "source_support": [
                        {
                            "quote": label + " was assessed",
                            "passage_id": "fixture-passage",
                        }
                    ],
                }
            ],
        }

    def evaluation(
        self, row, candidate, *, passed=True, relation="alias", qualifiers=None
    ):
        self.data["items.json"]["records"].append(row)
        result = {
            "decision": "MATCH",
            "candidate_id": candidate,
            "match_relation": relation,
            "qualifiers": qualifiers or [],
            "reason": "Synthetic test classification",
        }
        self.data["comparison.json"]["arms"]["synthetic-arm"]["records"].append(
            {
                "id": row["id"],
                "all_correct": passed,
                "identity_correct": passed,
                "prediction": result,
            }
        )

    def run_builder(self, *, records=2, mentions=2):
        self.data["manifest.json"] = {
            "source_records": records,
            "participating_mentions": mentions,
        }
        for name, value in self.data.items():
            (self.root / name).write_text(json.dumps(value))
        return build(self.root, expected_records=records, expected_mentions=mentions)

    def test_known_seed_alias_is_not_overridden_by_model_challenge(self):
        self.data["bootstrap-v2.json"] = {
            "entities": [
                {"key": "seed:known", "kind": "drug", "level": "not_applicable"}
            ],
            "rules": [
                {
                    "kind": "drug",
                    "alias": "Known drug",
                    "status": "confirmed",
                    "canonical_key": "seed:known",
                }
            ],
        }
        row = self.row("R1", "Known drug")
        self.data["rules.json"]["records"] = [
            {
                **row,
                "resolution": {
                    "decision": "MATCH",
                    "candidate_id": "seed:known",
                    "method": "unique_seed_name",
                },
            }
        ]
        challenge = {**row, "id": "C1", "cohort": "known_risky_synonym_challenge"}
        self.evaluation(challenge, "std:wrong", passed=True)
        payload = self.run_builder(records=1, mentions=1)
        self.assertEqual(payload["entities"], [])
        self.assertEqual(payload["rules"], [])
        self.assertEqual(len(payload["mentions"]), 1)
        self.assertEqual(payload["mentions"][0]["entity_ref"], "R1")

    def test_qualified_form_preserves_exact_condition_and_article_scope(self):
        self.data["bootstrap-v2.json"]["entities"] = [
            {"key": "seed:breast", "kind": "disease", "level": "main"}
        ]
        row = self.row("E1", "HER2-positive breast cancer", kind="disease")
        self.evaluation(
            row, "seed:breast", relation="qualified_form", qualifiers=["HER2-positive"]
        )
        payload = self.run_builder(records=1, mentions=1)
        rule = payload["rules"][0]
        self.assertEqual(rule["scope"]["type"], "article")
        self.assertEqual(rule["provenance"]["required_qualifiers"], ["HER2-positive"])
        self.assertEqual(
            payload["mentions"][0]["qualifiers"]["source_qualifiers"], ["HER2-positive"]
        )
        self.assertEqual(
            payload["mentions"][0]["qualifiers"]["source_support"][0]["quote"],
            row["mentions"][0]["source_support"][0]["quote"],
        )
        self.data["comparison.json"]["arms"]["synthetic-arm"]["records"][0][
            "prediction"
        ]["qualifiers"] = []
        with self.assertRaisesRegex(ValueError, "lost its source conditions"):
            self.run_builder(records=1, mentions=1)

    def test_cross_standard_ids_share_identity_and_keep_both_external_ids(self):
        rx, chem = "std:RXNORM:RXNORM:1", "std:CHEMBL:CHEMBL1"
        self.data["candidate-catalog.json"]["records"] = [
            {
                "id": rx,
                "namespace": "RXNORM",
                "identifier": "RXNORM:1",
                "label": "Drug A",
            },
            {
                "id": chem,
                "namespace": "CHEMBL",
                "identifier": "CHEMBL1",
                "label": "DRUG A",
            },
        ]
        # Reverse ordering must not defeat the RxNorm ingredient preference.
        self.data["reference.json"]["records"] = [
            {"id": "E1", "accepted": [{"candidate_id": chem}, {"candidate_id": rx}]}
        ]
        self.evaluation(self.row("E1", "Drug A"), chem)
        self.evaluation(self.row("E2", "Drug A alias", group="K2"), rx)
        payload = self.run_builder()
        self.assertEqual(len(payload["entities"]), 1)
        entity = payload["entities"][0]
        self.assertEqual(entity["key"], rx)
        self.assertEqual(
            entity["external_ids"], {"RXNORM": "RXNORM:1", "CHEMBL": "CHEMBL1"}
        )
        self.assertEqual({r["canonical_key"] for r in payload["rules"]}, {rx})

    def test_later_pass_promotes_existing_candidate_without_second_identity(self):
        candidate = {
            "id": "std:RXNORM:RXNORM:1",
            "namespace": "RXNORM",
            "identifier": "RXNORM:1",
            "label": "Drug A",
        }
        self.data["candidate-catalog.json"]["records"] = [candidate]
        self.evaluation(
            self.row("E1", "Uncertain term", candidates=[candidate]),
            candidate["id"],
            passed=False,
        )
        self.evaluation(self.row("E2", "Drug A", group="K2"), candidate["id"])
        payload = self.run_builder()
        self.assertEqual(len(payload["entities"]), 1)
        self.assertEqual(payload["entities"][0]["status"], "active")
        self.assertEqual(
            [r["status"] for r in payload["rules"]], ["proposed", "confirmed"]
        )

    def test_participation_counts_preserve_sources_without_eligible_entity(self):
        candidate = {
            "id": "std:RXNORM:RXNORM:1",
            "namespace": "RXNORM",
            "identifier": "RXNORM:1",
            "label": "Drug A",
        }
        self.data["candidate-catalog.json"]["records"] = [candidate]
        self.evaluation(self.row("E1", "Drug A"), candidate["id"])
        payload = self.run_builder(records=2, mentions=1)
        self.assertEqual(
            payload["source"]["coverage"],
            {
                "source_records": 2,
                "participating_mentions": 1,
                "participating_candidates": 1,
            },
        )
        self.data["manifest.json"]["source_records"] = 3
        (self.root / "manifest.json").write_text(json.dumps(self.data["manifest.json"]))
        with self.assertRaisesRegex(ValueError, "Frozen source coverage"):
            build(self.root, expected_records=2, expected_mentions=1)
        self.data["items.json"]["records"].append(self.data["items.json"]["records"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate participating mention"):
            self.run_builder(records=2, mentions=1)

    def test_two_empty_seed_names_retain_source_ids_without_alias(self):
        snapshot = {
            "source": {"kind": "fixture"},
            "drugs": [{"id": "drug-empty", "name": ""}],
            "tumors": [
                {"id": "brain", "name_cn": "脑胶质瘤"},
                {"id": "disease-empty", "name_cn": ""},
            ],
        }
        payload = seed_payload(snapshot, "f" * 64)
        unnamed = [r for r in payload["entities"] if r["provenance"]["name_missing"]]
        self.assertEqual(len(unnamed), 2)
        self.assertEqual({r["status"] for r in unnamed}, {"candidate"})
        self.assertEqual(
            {r["external_ids"]["tumor_api"] for r in unnamed},
            {"drug-empty", "disease-empty"},
        )
        self.assertFalse(
            {r["key"] for r in unnamed} & {r["canonical_key"] for r in payload["rules"]}
        )
        self.assertTrue(all(r["name"].startswith("源库未命名") for r in unnamed))

    def test_future_seed_filter_preserves_source_punctuation_but_omits_rule(self):
        snapshot = {
            "source": {},
            "drugs": [{"id": "a", "name": "Drug A", "aliases": ["-", "NA"]}],
            "tumors": [{"id": "brain", "name_cn": "脑胶质瘤"}],
        }
        legacy = seed_payload(snapshot, "f" * 64)
        future = seed_payload(snapshot, "f" * 64, exclude_placeholder_aliases=True)
        self.assertIn("-", [r["alias"] for r in legacy["rules"]])
        self.assertNotIn("-", [r["alias"] for r in future["rules"]])
        self.assertIn("NA", [r["alias"] for r in future["rules"]])
        self.assertEqual(legacy["entities"], future["entities"])
        self.assertNotEqual(legacy["batch_id"], future["batch_id"])

    def test_english_seed_name_can_supply_missing_chinese_display_name(self):
        payload = seed_payload(
            {
                "source": {},
                "drugs": [{"id": "english", "name_en": "Drug A"}],
                "tumors": [{"id": "brain", "name_cn": "脑胶质瘤"}],
            },
            "f" * 64,
        )
        entity = next(r for r in payload["entities"] if r["kind"] == "drug")
        self.assertEqual(entity["name"], "Drug A")
        self.assertEqual(entity["status"], "active")

    def test_reuse_extension_preserves_original_batch_and_names_its_sources(self):
        rule = {
            "kind": "disease",
            "alias": "metastatic breast cancer",
            "status": "confirmed",
            "canonical_key": "seed:breast",
            "origin": "model",
            "action": "alias",
            "scope": {"type": "article", "key": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            "reason": "Synthetic reference agreement",
            "provenance": {
                "match_relation": "qualified_form",
                "required_qualifiers": ["metastatic"],
            },
            "evidence": [{"quote": "metastatic breast cancer"}],
        }
        source = {
            "batch_id": "synthetic-original",
            "rules": [rule, {**rule, "alias": "mBC"}],
        }
        source_path = self.root / "literature-registry.json"
        source_path.write_text(json.dumps(source))
        (self.root / "bootstrap-v2.json").write_text(json.dumps({"rules": []}))
        before = source_path.read_bytes()
        payload = build_reuse(self.root)
        self.assertEqual(len(payload["rules"]), 1)
        promoted = payload["rules"][0]
        self.assertEqual(promoted["scope"], {"type": "global", "key": ""})
        self.assertEqual(promoted["provenance"]["required_qualifiers"], ["metastatic"])
        reference = promoted["provenance"]["source_rules"][0]
        self.assertEqual(reference["source_batch_id"], "synthetic-original")
        self.assertEqual(len(reference["source_rule_sha256"]), 64)
        self.assertEqual(reference["source_rule_identity"]["scope"]["type"], "article")
        self.assertEqual(payload["mentions"], [])
        self.assertEqual(source_path.read_bytes(), before)


FROZEN = (
    Path(__file__).resolve().parents[1] / "var/experiments/entity-resolution-20261101"
)


@unittest.skipUnless(
    (FROZEN / "manifest.json").exists(), "Frozen pilot data are local and not committed"
)
class FrozenRegistryInputTests(unittest.TestCase):
    def test_all_30_sources_and_89_participants_are_accounted_for(self):
        manifest = json.loads((FROZEN / "manifest.json").read_text())
        rules = json.loads((FROZEN / "rules.json").read_text())["records"]
        items = json.loads((FROZEN / "items.json").read_text())["records"]
        selected = rules + [r for r in items if r["cohort"] == "unresolved_after_rules"]
        mentions = [m for r in selected for m in r["mentions"]]
        self.assertEqual(manifest["source_records"], 30)
        self.assertEqual(manifest["participating_mentions"], 89)
        self.assertEqual(len(mentions), 89)
        self.assertEqual(
            len({(m["source_run"], m["group_id"], m["entity_ref"]) for m in mentions}),
            89,
        )
        # One source record has no drug/disease participant in this narrow step.
        self.assertEqual(len({(m["source_run"], m["group_id"]) for m in mentions}), 29)
        source_files = [
            Path(path)
            for path in manifest["source_files"]
            if path.endswith("delivery/bundle.json")
        ]
        if not all(path.exists() for path in source_files):
            self.skipTest("Original frozen bundle paths unavailable")
        bundles = [json.loads(path.read_text()) for path in source_files]
        self.assertEqual(sum(len(bundle["records"]) for bundle in bundles), 30)
        original_participants = {
            (bundle["source_run"], record["group_id"], entity["ref"])
            for bundle in bundles
            for record in bundle["records"]
            for entity in record["entities"]
            if (
                entity["kind"] == "drug"
                and "intervention"
                in record["fact"].get("entity_roles", {}).get(entity["ref"], [])
            )
            or (
                entity["kind"] == "disease"
                and "disease"
                in record["fact"].get("entity_roles", {}).get(entity["ref"], [])
            )
        }
        self.assertEqual(
            original_participants,
            {(m["source_run"], m["group_id"], m["entity_ref"]) for m in mentions},
        )

    def test_actual_bootstrap_retains_both_unnamed_source_rows(self):
        payload = json.loads((FROZEN / "bootstrap-v2.json").read_text())
        self.assertEqual(len(payload["entities"]), 1253)
        unnamed = [
            r for r in payload["entities"] if r["provenance"].get("name_missing")
        ]
        self.assertEqual(len(unnamed), 2)
        self.assertEqual({r["status"] for r in unnamed}, {"candidate"})
        self.assertFalse(
            {r["key"] for r in unnamed} & {r["canonical_key"] for r in payload["rules"]}
        )


if __name__ == "__main__":
    unittest.main()
