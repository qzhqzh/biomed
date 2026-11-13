import copy
import hashlib
import json
import tempfile
from pathlib import Path

from django.test import TestCase

from apps.curation.models import (
    Concept,
    DrugMoleculeLink,
    EntityTranslation,
    ResourceAssertion,
    ResourceDataset,
    ResourceEntity,
    SourceRelease,
)
from apps.curation.reference_resources import import_snapshot, import_translations


class ReferenceResourceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.release = SourceRelease.objects.create(
            namespace="RXNORM", version="test-rx", sha256="a" * 64, count=3, active=True
        )
        self.drug = Concept.objects.create(
            release=self.release, identifier="RXNORM:1", label="Drug A"
        )
        self.ambiguous = Concept.objects.create(
            release=self.release, identifier="RXNORM:2", label="Shared"
        )
        self.other = Concept.objects.create(
            release=self.release, identifier="RXNORM:3", label="Drug A sodium"
        )

    def write(self, filename, value):
        raw = json.dumps(value).encode()
        (self.directory / filename).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def chembl(self, *, missing_target=False):
        receipt = {
            "file": "raw.json",
            "url": "https://www.ebi.ac.uk/chembl/api/data/mechanism.json",
            "sha256": self.write("raw.json", {"test": True}),
        }
        molecules = [
            {
                "molecule_chembl_id": "CHEMBL1",
                "pref_name": "DRUG A",
                "molecule_synonyms": [{"molecule_synonym": "Shared"}],
                "molecule_hierarchy": {"parent_chembl_id": "CHEMBL1"},
            },
            {
                "molecule_chembl_id": "CHEMBL2",
                "pref_name": "OTHER",
                "molecule_synonyms": [{"molecule_synonym": "Shared"}],
                "molecule_hierarchy": {"parent_chembl_id": "CHEMBL2"},
            },
        ]
        mechanisms = [
            {
                "mec_id": 1,
                "molecule_chembl_id": "CHEMBL1",
                "target_chembl_id": "CHEMBL99" if missing_target else "CHEMBL3",
                "action_type": "INHIBITOR",
                "mechanism_of_action": "Target inhibitor",
                "disease_efficacy": 1,
                "_source": receipt,
            },
            {
                "mec_id": 2,
                "molecule_chembl_id": "CHEMBL1",
                "target_chembl_id": None,
                "action_type": None,
                "mechanism_of_action": "Unresolved target",
                "_source": receipt,
            },
        ]
        targets = [
            {
                "target_chembl_id": "CHEMBL3",
                "pref_name": "Complex",
                "organism": "Homo sapiens",
                "target_type": "PROTEIN COMPLEX",
                "target_components": [{"accession": "P1"}, {"accession": "P2"}],
            }
        ]
        files = {}
        for name, rows in (
            ("molecules", molecules),
            ("mechanisms", mechanisms),
            ("targets", targets),
        ):
            files[name] = {
                "file": name + ".json",
                "sha256": self.write(name + ".json", rows),
                "count": len(rows),
            }
        status = {
            "status": "UP",
            "chembl_db_version": "ChEMBL_37",
            "chembl_release_date": "2026-05-01",
        }
        manifest = {
            "schema": "chembl-mechanism-snapshot-v1",
            "state": "completed",
            "version": "ChEMBL_37",
            "release_date": "2026-05-01",
            "status_before": status,
            "status_after": copy.deepcopy(status),
            "files": files,
            "sources": [receipt],
            "counts": {n: r["count"] for n, r in files.items()},
        }
        self.write("manifest.json", manifest)
        return manifest

    def rxclass(self):
        raw_sha = self.write("raw.json", {"member": "Drug A"})
        members = [
            {
                "rxcui": x,
                "class_id": "N1",
                "relation": "has_epc",
                "relation_source": "FDASPL",
                "direct": True,
                "tty": "IN",
                "source_url": "https://rxnav.nlm.nih.gov/REST/rxclass/classMembers.json",
                "raw_sha256": raw_sha,
                "raw_members": [],
            }
            for x in ("1", "999")
        ]
        outputs = {
            "classes.json": self.write(
                "classes.json",
                [{"id": "N1", "name": "Kinase inhibitor", "class_type": "EPC"}],
            ),
            "memberships.json": self.write("memberships.json", members),
        }
        manifest = {
            "schema_version": "rxclass-epc-snapshot-v1",
            "state": "completed",
            "version": "MEDRT test",
            "versions": {"FDASPL": "MEDRT test", "RXNORM": "test-rx"},
            "versions_after": {"FDASPL": "MEDRT test", "RXNORM": "test-rx"},
            "output_sha256": outputs,
            "requests": [
                {"state": "validated", "raw_file": "raw.json", "raw_sha256": raw_sha}
            ],
            "counts": {"classes_total": 1, "classes_completed": 1, "memberships": 2},
        }
        self.write("manifest.json", manifest)
        return manifest

    def test_rxclass_preserves_unmatched_and_is_idempotent(self):
        self.rxclass()
        first = import_snapshot(self.directory)
        second = import_snapshot(self.directory)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.counts["local_ingredients"], 1)
        self.assertEqual(second.counts["unmatched_ingredients"], 1)
        self.assertEqual(ResourceAssertion.objects.count(), 2)
        response = self.client.get(
            "/api/v1/curation/drug-resources/", {"identifier": "RXNORM:1"}
        ).json()
        self.assertEqual(response["classes"][0]["label"], "Kinase inhibitor")
        self.assertIs(response["classes"][0]["direct"], True)

    def test_chembl_exact_candidate_ambiguity_null_target_and_complex(self):
        self.chembl()
        dataset = import_snapshot(self.directory)
        self.assertEqual(dataset.counts["rxnorm_name_candidates"], 1)
        self.assertEqual(dataset.counts["rxnorm_ambiguous_names"], 1)
        self.assertFalse(
            DrugMoleculeLink.objects.filter(ingredient=self.other).exists()
        )
        data = self.client.get(
            "/api/v1/curation/drug-resources/", {"identifier": "RXNORM:1"}
        ).json()
        molecule = data["molecules"][0]
        self.assertEqual(molecule["mapping"]["status"], "candidate")
        mechanisms = {m["id"]: m for m in molecule["mechanisms"]}
        self.assertEqual(len(mechanisms["1"]["target"]["components"]), 2)
        self.assertIsNone(mechanisms["2"]["target"])
        self.assertFalse(
            self.client.get(
                "/api/v1/curation/drug-resources/", {"identifier": "RXNORM:2"}
            ).json()["molecules"]
        )

    def test_partial_or_corrupt_snapshot_keeps_current_version(self):
        manifest = self.rxclass()
        current = import_snapshot(self.directory)
        manifest["state"] = "paused"
        self.write("manifest.json", manifest)
        with self.assertRaisesMessage(ValueError, "completed"):
            import_snapshot(self.directory)
        manifest["state"] = "completed"
        self.write("manifest.json", manifest)
        (self.directory / "memberships.json").write_text("[]")
        with self.assertRaisesMessage(ValueError, "checksum"):
            import_snapshot(self.directory)
        self.assertTrue(ResourceDataset.objects.get(pk=current.pk).active)
        self.assertEqual(ResourceDataset.objects.count(), 1)

    def test_dangling_target_rejected_before_staging(self):
        self.chembl(missing_target=True)
        with self.assertRaisesMessage(ValueError, "Dangling"):
            import_snapshot(self.directory)
        self.assertEqual(ResourceDataset.objects.count(), 0)

    def test_manifest_version_must_match_authoritative_status(self):
        for source in (self.chembl, self.rxclass):
            manifest = source()
            manifest["version"] = "made-up-version"
            self.write("manifest.json", manifest)
            with self.assertRaisesMessage(ValueError, "version mismatch"):
                import_snapshot(self.directory)
        self.assertFalse(ResourceDataset.objects.exists())

    def test_new_complete_snapshot_switches_without_deleting_history(self):
        self.rxclass()
        old = import_snapshot(self.directory)
        manifest = self.rxclass()
        manifest["version"] = "MEDRT new"
        manifest["versions"]["FDASPL"] = "MEDRT new"
        manifest["versions_after"]["FDASPL"] = "MEDRT new"
        self.write("manifest.json", manifest)
        new = import_snapshot(self.directory)
        self.assertNotEqual(old.pk, new.pk)
        self.assertFalse(ResourceDataset.objects.get(pk=old.pk).active)
        self.assertTrue(new.active)
        self.assertEqual(ResourceEntity.objects.count(), 2)

    def test_translations_search_and_release_isolation(self):
        row = {
            "identifier": "RXNORM:1",
            "en": "Drug A",
            "zh": "药物甲",
            "provider": "test",
            "status": "reference",
            "source_version": "test-rx",
            "source_url": "https://example.com/reference",
            "match_basis": "id_and_name",
        }
        self.write("translations.json", [row])
        path = self.directory / "translations.json"
        import_translations(path, "RXNORM")
        import_translations(path, "RXNORM")
        self.assertEqual(EntityTranslation.objects.count(), 1)
        data = self.client.get(
            "/api/v1/curation/concepts/", {"namespace": "RXNORM", "q": "药物甲"}
        ).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["translations"][0]["label"], "药物甲")
        self.release.active = False
        self.release.save(update_fields=["active"])
        current = SourceRelease.objects.create(
            namespace="RXNORM", version="new", sha256="b" * 64, count=1, active=True
        )
        Concept.objects.create(
            release=current, identifier="RXNORM:1", label="Changed identity"
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/curation/concepts/", {"namespace": "RXNORM", "q": "药物甲"}
            ).json()["total"],
            0,
        )
        with self.assertRaisesMessage(ValueError, "identity"):
            import_translations(path, "RXNORM")
        self.assertEqual(EntityTranslation.objects.count(), 1)
