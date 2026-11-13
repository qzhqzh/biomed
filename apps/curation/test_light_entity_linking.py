import unittest

from apps.curation.light_entity_linking import load_index, retrieve


class LightEntityLinkingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "releases": {
                "rx": {"namespace": "RXNORM", "version": "test-rx", "sha256": "rx-sha"},
                "mondo": {
                    "namespace": "MONDO",
                    "version": "test-mondo",
                    "sha256": "mondo-sha",
                },
            },
            "concepts": [
                {
                    "release_id": "rx",
                    "identifier": "RXNORM:1",
                    "label": "Drug-A",
                    "synonyms": ["DA", "shared"],
                },
                {
                    "release_id": "rx",
                    "identifier": "RXNORM:2",
                    "label": "Drug-B",
                    "synonyms": ["shared"],
                },
                {
                    "release_id": "rx",
                    "identifier": "RXNORM:1",
                    "label": "Drug-A",
                    "synonyms": ["DA"],
                },
                {
                    "release_id": "mondo",
                    "identifier": "MONDO:1",
                    "label": "Test disease",
                    "synonyms": ["DA"],
                },
            ],
            "gene_version": {"release": "test-gene", "genes_sha256": "gene-sha"},
            "genes": [
                {
                    "gene_id": 1,
                    "symbol": "GENE1",
                    "name": "test gene",
                    "synonyms": ["old-name"],
                }
            ],
        }
        self.index = load_index(self.snapshot)

    def test_same_id_is_deduplicated_and_alias_evidence_retained(self):
        result = retrieve(self.index, "drug", "DA", aliases=["Drug-A"])
        self.assertEqual(result["status"], "unique_candidate")
        self.assertEqual(result["total_candidates"], 1)
        self.assertEqual(result["candidates"][0]["id"], "RXNORM:1")
        self.assertEqual(len(result["candidates"][0]["matching_evidence"]), 2)
        self.assertIsNone(result["confirmed_id"])

    def test_topk_does_not_hide_ambiguity(self):
        result = retrieve(self.index, "drug", "shared", limit=1)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["total_candidates"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_preferred_label_does_not_hide_other_same_namespace_candidates(self):
        # Like "lung cancer": one preferred label can also be another
        # concept's synonym. The caller must see this identity ambiguity.
        self.snapshot["concepts"][1]["synonyms"].append("Drug-A")
        result = retrieve(load_index(self.snapshot), "drug", "Drug-A")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["total_candidates"], 2)
        self.assertEqual(result["candidates"][0]["id"], "RXNORM:1")
        self.assertIsNone(result["confirmed_id"])

    def test_types_and_combinations_do_not_cross_match(self):
        self.assertEqual(
            retrieve(self.index, "disease", "DA")["candidates"][0]["id"], "MONDO:1"
        )
        self.assertEqual(retrieve(self.index, "gene", "DA")["status"], "unresolved")
        for mention in (
            "Drug-A + Drug-B",
            "Drug-A and Drug-B",
            "Drug-A/Drug-B",
            "Drug-A (combination)",
        ):
            self.assertEqual(
                retrieve(self.index, "drug", mention)["status"], "unresolved"
            )

    def test_conservative_normalization_and_protein_link_level(self):
        result = retrieve(self.index, "drug", "  DRUG–A  ")
        self.assertEqual(result["candidates"][0]["id"], "RXNORM:1")
        self.assertEqual(
            result["candidates"][0]["matching_evidence"][0]["match_method"],
            "normalized_whole_name",
        )
        protein = retrieve(self.index, "protein", "old-name")["candidates"][0]
        self.assertEqual(protein["link_level"], "associated_gene_not_protein_isoform")
        self.assertTrue(protein["requires_identity_review"])

    def test_unsupported_and_unknown_are_not_new_entities(self):
        self.assertEqual(
            retrieve(self.index, "treatment", "Drug-A")["status"], "unsupported_type"
        )
        self.assertEqual(retrieve(self.index, "drug", "unknown")["candidates"], [])
        disease = retrieve(self.index, "disease", "DA")["candidates"][0]
        self.assertEqual(disease["synonym_scope"], "not_preserved")
        with self.assertRaises(TypeError):
            retrieve(self.index, "drug", "DA", aliases="Drug-A")

    def test_additional_sources_preserve_namespaces_versions_and_aliases(self):
        index = load_index(
            self.snapshot,
            oncotree={
                "version": "tree-test",
                "nodes": [
                    {
                        "identifier": "ONCOTREE:TD",
                        "code": "TD",
                        "en": "Test disease",
                        "level": 2,
                    }
                ],
            },
            chembl=[
                {
                    "molecule_chembl_id": "CHEMBL1",
                    "pref_name": "Drug-A",
                    "molecule_synonyms": [
                        {"molecule_synonym": "new-code", "synonyms": "DA"}
                    ],
                }
            ],
        )
        drug = retrieve(index, "drug", "Drug-A")
        self.assertEqual(drug["status"], "ambiguous")
        self.assertEqual(
            {c["namespace"] for c in drug["candidates"]}, {"RXNORM", "CHEMBL"}
        )
        chembl = retrieve(index, "drug", "new-code")["candidates"][0]
        self.assertEqual(chembl["version"], "37")
        self.assertEqual(len(chembl["source_sha256"]), 64)
        tree = retrieve(index, "disease", "TD")["candidates"][0]
        self.assertEqual(tree["id"], "ONCOTREE:TD")
        self.assertEqual(tree["version"], "tree-test")


if __name__ == "__main__":
    unittest.main()
