"""Synthetic fixtures: no source, candidate, or target identity is real."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.curation.knowledge_export import digest, export_bundle
from apps.curation.knowledge_target_mapping import load_gene_standard, prepare_mappings
from apps.curation.research_history import read_record, register_record
from scripts.publish_literature_batch import candidate_id, file_sha, main, make_record

DRUG = "11111111-1111-4111-8111-111111111111"
TUMOR = "22222222-2222-4222-8222-222222222222"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LiteratureBatchPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dictionary = {
            "schema_version": "tumor-api-dictionary-v1",
            "source": {"snapshot_id": "synthetic-fixture", "read_only": True,
                       "target_written": False, "read_at_utc": "2026-11-01T00:00:00Z"},
            "drugs": [{"id": DRUG, "name": "Drug A", "aliases": []}],
            "tumors": [{"id": TUMOR, "name": "Cancer X", "aliases": []}],
            "rules": [], "logical_targets": [],
        }
        self.dictionary_path = self.root / "dictionary.json"
        write(self.dictionary_path, self.dictionary)

    def fixture(self, run, pmid, *, extra_articles=(), receipt=True, parent=None):
        final = (parent or self.root) / run / "final"
        quote = "Patients with Cancer X received Drug A."
        anchors = [{"purpose": "evidence", "passage_id": f"{i:08d}-1111-4111-8111-111111111111",
                    "locator": f"body/p:{i}", "start": 0, "end": len(quote), "quote": quote}
                   for i in range(1, 5)]
        row = {"group_id": "synthetic-group", "version_id": "44444444-4444-4444-8444-444444444444",
               "source_sha256": "a" * 64, "article": {"pmid": pmid, "title": "Synthetic source", "doi": ""},
               "fact": {"relation": "treatment_outcome", "population": "Cancer X", "treatment": "Drug A",
                        "entity_roles": {"drug": ["intervention"], "cancer": ["disease"]}},
               "entities": [{"ref": "drug", "kind": "drug", "label": "Drug A"},
                            {"ref": "cancer", "kind": "disease", "label": "Cancer X"}],
               "anchors": anchors, "assessment": {"quality": {"status": "unreviewed"}}}
        bundle = {"schema_version": "knowledge-candidates-v1", "source_run": run, "records": [row]}
        mappings, report = prepare_mappings(bundle, self.dictionary)
        exported = export_bundle(bundle, dictionary=self.dictionary, mappings=mappings)
        articles = [{"pmid": pmid, "status": "classified", "category": "solid"}]
        articles.extend({"pmid": value, "status": "classified", "category": "non_neoplastic"}
                        for value in extra_articles)
        summary = {"state": "batch_completed", "articles": articles, "knowledge_records": 1,
                   "compatibility_counts": exported["counts"], "offline_ready_count": exported["offline_ready_count"],
                   "target_written": False, "submit_ready": False}
        for name, value in (("knowledge.json", bundle), ("target-mappings.json", mappings),
                            ("target-mapping-report.json", report), ("tumor-api-export.json", exported),
                            ("summary.json", summary)):
            write(final / name, value)
        if receipt:
            result = {"source_run": run, "import_sha256": digest(bundle), "dry_run": False,
                      "candidate_count": 1, "entity_registry": {},
                      "candidates": [{"group_id": "synthetic-group", "id": candidate_id(run, "synthetic-group")}]}
            write(final / "import-receipt.json", {"bundle_sha256": file_sha(final / "knowledge.json"),
                  "target_written": False, "published": False, "result": result})
        return final

    def build(self, finals, **kwargs):
        return make_record(finals, self.dictionary_path, "synthetic-report", "Synthetic report", **kwargs)

    def replace_bundle(self, final, bundle):
        mappings, report = prepare_mappings(bundle, self.dictionary)
        exported = export_bundle(bundle, dictionary=self.dictionary, mappings=mappings)
        for name, value in (("knowledge.json", bundle), ("target-mappings.json", mappings),
                            ("target-mapping-report.json", report), ("tumor-api-export.json", exported)):
            write(final / name, value)
        summary = json.loads((final / "summary.json").read_text())
        summary.update(knowledge_records=len(bundle["records"]), compatibility_counts=exported["counts"],
                       offline_ready_count=exported["offline_ready_count"])
        write(final / "summary.json", summary)
        receipt = json.loads((final / "import-receipt.json").read_text())
        receipt["bundle_sha256"] = file_sha(final / "knowledge.json")
        receipt["result"].update(import_sha256=digest(bundle), candidate_count=len(bundle["records"]),
                                 candidates=[{"group_id": row["group_id"],
                                              "id": candidate_id(bundle["source_run"], row["group_id"])}
                                             for row in bundle["records"]])
        write(final / "import-receipt.json", receipt)

    def routing_fixture(self):
        from scripts.run_literature_batch import effective_routing

        final = self.fixture("ten-paper-closure-20261102", "101", extra_articles=("102", "103"))
        root = final.parent
        original = {"records": [{"pmid": "101", "status": "classified", "category": "solid"},
                                {"pmid": "102", "status": "classified", "category": "non_neoplastic"},
                                {"pmid": "103", "status": "classified", "category": "non_neoplastic"}],
                    "solid_pmids": ["101"], "categories": {"solid": 1, "non_neoplastic": 2}}
        write(root / "classification/selection.json", original)
        fallback = {"records": [{"pmid": "101", "original_status": "classified", "original_category": "solid",
                                 "fallback_status": "already_solid"},
                                {"pmid": "102", "original_status": "classified", "original_category": "non_neoplastic",
                                 "fallback_status": "classified", "fallback_category": "solid"},
                                {"pmid": "103", "original_status": "classified", "original_category": "non_neoplastic",
                                 "fallback_status": "input_too_long"}], "continue_pmids": ["102"]}
        fallback_path = root / "fallback-fulltext-v1/routing.json"
        write(fallback_path, fallback)
        routed = {**original, "solid_pmids": ["101", "102"], "fulltext_fallback": str(fallback_path.parent),
                  "fallback_routing_sha256": file_sha(fallback_path)}
        write(root / "classification-routing.json", routed)
        effective = effective_routing(root, original, routed, fallback)
        write(root / "selection.json", {"requested_pmids": ["101", "102"]})
        summary = json.loads((final / "summary.json").read_text())
        summary["articles"] = effective["records"]
        write(final / "summary.json", summary)
        return final, original, fallback, effective

    def test_multiple_batches_keep_excluded_articles_and_all_purpose_labels(self):
        main_final = self.fixture("ten-paper-closure-20261102", "101", extra_articles=("102",))
        positive = self.fixture("target-positive-closure-20261102", "103")
        followup = self.fixture("ten-paper-closure-20261102-fulltext-followup", "102")
        record, missing = self.build([main_final, positive, followup])
        self.assertEqual(missing, [])
        self.assertEqual(record["sample_count"], 3)
        self.assertEqual(record["sample_unit"], "篇")
        self.assertIn("主批 · 2 篇", record["target_compatibility"]["batch_labels"].values())
        self.assertIn("独立分子适配对照 · 1 篇", record["target_compatibility"]["batch_labels"].values())
        self.assertIn("全文补判或补充批次 · 1 篇", record["target_compatibility"]["batch_labels"].values())
        self.assertIn("其他已分类 1 篇", record["dataset"]["notes"][0])
        row = record["target_compatibility"]["records"][0]
        self.assertEqual(row["candidate_id"], candidate_id("ten-paper-closure-20261102", "synthetic-group"))
        self.assertEqual(row["quality"], {"status": "unreviewed"})
        self.assertEqual(len(row["quotes"]), 3)
        self.assertTrue(all(m["roles"] and m["basis"] and m["accepted_by_exporter"] for m in row["entity_mappings"]))
        self.assertTrue(all(m["target_name"] for m in row["entity_mappings"]))
        export = json.loads((main_final / "tumor-api-export.json").read_text())['records'][0]
        self.assertEqual(row["trigger_scope"], export["adaptation"]["trigger_scope"])
        self.assertEqual(row["treatment_scope"], export["adaptation"]["treatment_scope"])
        self.assertEqual(row["matching_validation"], export["matching_validation"])
        self.assertEqual(self.build([main_final, positive, followup])[0], record)

    def test_missing_receipt_only_writes_offline_preview_and_never_registers(self):
        final = self.fixture("ten-paper-closure-20261102", "101", receipt=False)
        record, missing = self.build([final])
        self.assertEqual(missing, [str(final)])
        self.assertIsNone(record["target_compatibility"]["records"][0]["candidate_id"])
        self.assertEqual(record["registration_state"], "waiting_local_import")
        output = self.root / "offline.json"
        argv = ["publish_literature_batch", "--final", str(final), "--dictionary", str(self.dictionary_path),
                "--id", "synthetic-report", "--title", "Synthetic report", "--output", str(output), "--register"]
        with patch("sys.argv", argv), patch("scripts.publish_literature_batch.register_record") as register:
            with self.assertRaisesRegex(ValueError, "receipts missing"):
                main()
            register.assert_not_called()
        self.assertTrue(output.exists())
        self.assertEqual(json.loads(output.read_text())["registration_state"], "waiting_local_import")

    def test_rejects_tampered_source_mappings_export_and_unbound_report(self):
        for filename, mutate, message in (
            ("knowledge.json", lambda d: d["records"][0]["fact"].update(treatment="Changed"), "bundle SHA"),
            ("target-mappings.json", lambda d: d["synthetic-group"].update(basis="Changed"), "mappings SHA"),
            ("tumor-api-export.json", lambda d: d["records"][0].update(offline_ready=True), "export does not reproduce"),
            ("target-mapping-report.json", lambda d: d["records"][0]["entity_mappings"][0].update(detail="Changed"), "report does not reproduce"),
            ("summary.json", lambda d: d.update(knowledge_records=99), "knowledge count differs"),
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                original = self.root
                self.root = Path(folder)
                try:
                    final = self.fixture("ten-paper-closure-20261102", "101")
                    path = final / filename
                    value = json.loads(path.read_text())
                    mutate(value)
                    write(path, value)
                    with self.assertRaisesRegex(ValueError, message):
                        self.build([final])
                finally:
                    self.root = original

    def test_rejects_fake_candidate_and_dry_run_receipts(self):
        for mutation, message in (
            (lambda r: r["result"]["candidates"][0].update(id="00000000-0000-4000-8000-000000000001"), "candidate ID"),
            (lambda r: r["result"].update(dry_run=True), "real local import"),
            (lambda r: r["result"].update(import_sha256="0" * 64), "payload SHA"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as folder:
                original = self.root
                self.root = Path(folder)
                try:
                    final = self.fixture("ten-paper-closure-20261102", "101")
                    path = final / "import-receipt.json"
                    receipt = json.loads(path.read_text())
                    mutation(receipt)
                    write(path, receipt)
                    with self.assertRaisesRegex(ValueError, message):
                        self.build([final])
                finally:
                    self.root = original

    def test_rejects_changed_dictionary(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        changed = copy.deepcopy(self.dictionary)
        changed["drugs"][0]["name"] = "Other"
        write(self.dictionary_path, changed)
        with self.assertRaisesRegex(ValueError, "dictionary snapshot SHA"):
            self.build([final])

    def test_frozen_registry_and_gene_standard_are_required_and_replayed(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        registry_path = self.root / "shared-registry.json"
        registry = {"schema_version": "biomed.entity-registry.snapshot.v1", "source": {"read_only": True},
                    "entities": [], "rules": []}
        write(registry_path, registry)
        genes_path = self.root / "genes.jsonl"
        genes_path.write_text(json.dumps({"symbol": "TEST", "type": "protein-coding", "hgnc_id": "1", "gene_id": 1}) + "\n")
        bundle = json.loads((final / "knowledge.json").read_text())
        mappings, report = prepare_mappings(bundle, self.dictionary, registry,
                                           gene_standard=load_gene_standard(genes_path))
        exported = export_bundle(bundle, dictionary=self.dictionary, mappings=mappings)
        write(final / "target-mappings.json", mappings)
        write(final / "target-mapping-report.json", report)
        write(final / "tumor-api-export.json", exported)
        with self.assertRaisesRegex(ValueError, "Missing frozen registry"):
            self.build([final], genes_path=genes_path)
        record, missing = self.build([final], registry_paths=[registry_path], genes_path=genes_path)
        self.assertEqual(missing, [])
        self.assertEqual(record["sample_count"], 1)
        genes_path.write_text(genes_path.read_text().replace('"gene_id": 1', '"gene_id": 2'))
        with self.assertRaisesRegex(ValueError, "Gene standard snapshot SHA"):
            self.build([final], registry_paths=[registry_path], genes_path=genes_path)

    def test_summary_ledger_must_match_available_classification_source(self):
        final = self.fixture("ten-paper-closure-20261102", "101", extra_articles=("102",))
        summary = json.loads((final / "summary.json").read_text())
        write(final.parent / "classification/selection.json", {"records": summary["articles"]})
        self.assertEqual(self.build([final])[0]["sample_count"], 2)
        summary["articles"][1]["category"] = "solid"
        write(final / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "classification ledger differs"):
            self.build([final])

    def test_eligibility_and_execution_dimensions_are_counted_independently(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        bundle = json.loads((final / "knowledge.json").read_text())
        original = bundle["records"][0]
        states = [("complete", "eligible"), ("partial", "eligible"), ("failed", "deferred"),
                  ("complete", "excluded"), (None, None)]
        bundle["records"] = []
        for index, (execution, eligibility) in enumerate(states):
            row = copy.deepcopy(original)
            row["group_id"] = f"synthetic-{index}"
            row["assessment"] = {"quality": {"status": "unreviewed"}}
            if execution is not None:
                row["assessment"]["execution"] = {"status": execution}
            if eligibility is not None:
                row["assessment"]["eligibility"] = {"status": eligibility}
            bundle["records"].append(row)
        self.replace_bundle(final, bundle)
        record, _ = self.build([final])
        values = {item["label"]: item["value"] for item in record["methods"][0]["metrics"]}
        for label, value in (("提取记录", 5), ("结构合格候选", 2), ("结构待处理", 1),
                             ("结构排除", 1), ("结构未知", 1), ("执行完成", 2),
                             ("执行部分完成", 1), ("执行失败", 1), ("执行未知", 1)):
            self.assertEqual(values[f"主批 · {label}"], value)
        self.assertIn("5 条提取记录", record["summary"])
        self.assertIn("结构与执行状态分别统计", record["summary"])
        rows = record["target_compatibility"]["records"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1]["eligibility"]["status"], "eligible")
        self.assertEqual(rows[1]["execution"]["status"], "partial")
        self.assertEqual(rows[2]["execution"]["status"], "failed")
        self.assertIn("结构合格不等于语义正确", rows[1]["review_points"][1])

    def test_observations_attach_to_exact_group_without_changing_facts_or_status(self):
        main_final = self.fixture("ten-paper-closure-20261102", "101")
        other_final = self.fixture("target-positive-closure-20261102", "102")
        before, _ = self.build([main_final, other_final])
        source_sha = file_sha(main_final / "knowledge.json")
        observations = self.root / "observations.json"
        write(observations, {"schema_version": "biomed.research-observations.v1", "observations": [
            {"source_run": "ten-paper-closure-20261102", "group_id": "synthetic-group",
             "note": "Synthetic population mismatch; the original fact is unchanged.",
             "result_index": 3, "index_base": 0,
             "evidence": [{"version_id": "v1", "passage_id": "p1", "locator": "body/p:18",
                           "start": 0, "end": 8, "quote": "Eligible"}]},
            {"source_run": "ten-paper-closure-20261102", "group_id": "synthetic-group",
             "note": "Second synthetic observation."},
        ]})
        after, _ = self.build([main_final, other_final], observations_path=observations)
        old, new = before["target_compatibility"]["records"], after["target_compatibility"]["records"]
        self.assertEqual(old[1], new[1])
        self.assertEqual({key: value for key, value in old[0].items() if key != "review_points"},
                         {key: value for key, value in new[0].items() if key != "review_points"})
        self.assertEqual(old[0]["review_points"], new[0]["review_points"][:-2])
        self.assertIn("Synthetic population mismatch", new[0]["review_points"][-2])
        self.assertIn("结果索引 3，从 0 起算", new[0]["review_points"][-2])
        self.assertIn("body/p:18", new[0]["review_points"][-2])
        self.assertIn("字符 [0, 8)", new[0]["review_points"][-2])
        self.assertIn("原文：Eligible", new[0]["review_points"][-2])
        self.assertIn("Second synthetic observation", new[0]["review_points"][-1])
        self.assertTrue(any("Synthetic population mismatch" in finding and "synthetic-group" in finding
                            for finding in after["findings"]))
        self.assertTrue(any("Second synthetic observation" in finding for finding in after["findings"]))
        provenance = next(row for row in after["provenance"] if row["artifact"] == str(observations))
        self.assertEqual(provenance["sha256"], file_sha(observations))
        self.assertIn(provenance["sha256"], after["identity"]["reference_versions"])
        self.assertEqual(file_sha(main_final / "knowledge.json"), source_sha)
        output = self.root / "observations-preview.json"
        argv = ["publish_literature_batch", "--final", str(main_final), "--dictionary", str(self.dictionary_path),
                "--id", "synthetic-report", "--title", "Synthetic report", "--output", str(output),
                "--observations", str(observations)]
        with patch("sys.argv", argv), patch("scripts.publish_literature_batch.register_record") as register:
            main()
            register.assert_not_called()
        self.assertIn("Synthetic population mismatch", output.read_text())

    def test_observations_reject_unknown_groups_and_invalid_source_evidence(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        path = self.root / "observations.json"
        valid = {"source_run": "ten-paper-closure-20261102", "group_id": "synthetic-group", "note": "Observed."}
        for change, message in (({"source_run": "other-run"}, "unknown source run/group"),
                                ({"group_id": "other-group"}, "unknown source run/group"),
                                ({"note": " "}, "nonempty note"),
                                ({"evidence": [{"start": 9, "end": 2}]}, "evidence offsets")):
            with self.subTest(change=change):
                write(path, {"observations": [{**valid, **change}]})
                with self.assertRaisesRegex(ValueError, message):
                    self.build([final], observations_path=path)

    def test_completed_model_metrics_preserve_values_scope_and_source(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        path = final.parent / "classification/extract/summary.json"
        write(path, {"state": "completed", "processed": 7, "total_inference_seconds": 3.25,
                     "nvml_process_peak_mib": 1234.5, "timing_scope": "warm inference only",
                     "timing_resumed": True})
        record, _ = self.build([final])
        metrics = {item["label"]: item for item in record["methods"][0]["metrics"]}
        self.assertEqual(metrics["主批 · 元数据分类 · 已处理输入"]["value"], 7)
        inference = metrics["主批 · 元数据分类 · 推理累计（秒）"]
        self.assertEqual(inference["value"], 3.25)
        self.assertIn("不是包含加载、预热和暂停的总墙钟时间", inference["note"])
        self.assertIn("warm inference only", inference["note"])
        self.assertIn("断点续跑", inference["note"])
        peak = metrics["主批 · 元数据分类 · NVML 进程峰值（MiB）"]
        self.assertEqual(peak["value"], 1234.5)
        self.assertIn("断点续跑", peak["note"])
        source = next(item for item in record["provenance"] if item["artifact"] == str(path))
        self.assertEqual(source["sha256"], file_sha(path))
        self.assertIn(source["sha256"], record["identity"]["sample_versions"])

    def test_model_metrics_skip_unfinished_or_invalid_values_and_keep_valid_zero(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        root = final.parent
        running = root / "screening/qwen35-9b/summary.json"
        write(running, {"state": "running", "processed": 12, "total_inference_seconds": 4,
                        "nvml_process_peak_mib": 5678})
        entities = root / "stages/entities/extract/summary.json"
        write(entities, {"state": "completed", "processed": 2})
        relations = root / "stages/relations/extract/summary.json"
        write(relations, {"state": "completed", "processed": True, "total_inference_seconds": 0,
                          "nvml_process_peak_mib": -1})
        fallback = root / "fallback-fulltext-v1/extract/summary.json"
        write(fallback, {"state": "completed", "processed": 1,
                         "total_inference_seconds": float("nan"), "nvml_process_peak_mib": 0})
        record, _ = self.build([final])
        metrics = {item["label"]: item["value"] for item in record["methods"][0]["metrics"]}
        self.assertFalse(any("全文段落筛选" in label for label in metrics))
        self.assertEqual(metrics["主批 · 实体抽取 · 已处理输入"], 2)
        self.assertNotIn("主批 · 实体抽取 · 推理累计（秒）", metrics)
        self.assertNotIn("主批 · 实体抽取 · NVML 进程峰值（MiB）", metrics)
        self.assertNotIn("主批 · 关系抽取 · 已处理输入", metrics)
        self.assertEqual(metrics["主批 · 关系抽取 · 推理累计（秒）"], 0)
        self.assertNotIn("主批 · 关系抽取 · NVML 进程峰值（MiB）", metrics)
        self.assertEqual(metrics["主批 · 全文补判 · 已处理输入"], 1)
        self.assertNotIn("主批 · 全文补判 · 推理累计（秒）", metrics)
        self.assertEqual(metrics["主批 · 全文补判 · NVML 进程峰值（MiB）"], 0)
        artifacts = {item["artifact"] for item in record["provenance"]}
        self.assertNotIn(str(running), artifacts)
        self.assertTrue({str(entities), str(relations), str(fallback)} <= artifacts)

    def test_matching_effective_routing_uses_fulltext_and_preserves_unresolved(self):
        final, _, _, _ = self.routing_fixture()
        record, _ = self.build([final])
        self.assertEqual(record["sample_count"], 3)
        self.assertIn("有效分类（含全文补判）：实体瘤 2 篇，其他已分类 0 篇，未完成或待定 1 篇",
                      record["dataset"]["notes"][0])
        artifacts = {row["artifact"] for row in record["provenance"]}
        self.assertIn(str(final.parent / "classification-routing-v2.json"), artifacts)
        self.assertIn(str(final.parent / "fallback-fulltext-v1/routing.json"), artifacts)
        path = final.parent / "classification-routing-v2.json"
        effective = json.loads(path.read_text())
        effective["records"][1]["effective_category"] = "hematologic"
        write(path, effective)
        with self.assertRaisesRegex(ValueError, "routing ledger does not reproduce"):
            self.build([final])

    def test_effective_routing_rejects_tampered_sources_and_missing_required_route(self):
        final, original, _, _ = self.routing_fixture()
        fallback_path = final.parent / "fallback-fulltext-v1/routing.json"
        fallback = json.loads(fallback_path.read_text())
        fallback["records"][1]["fallback_category"] = "hematologic"
        write(fallback_path, fallback)
        with self.assertRaisesRegex(ValueError, "routing provenance SHA"):
            self.build([final])
        (final.parent / "classification-routing-v2.json").unlink()
        summary = json.loads((final / "summary.json").read_text())
        summary["articles"] = original["records"]
        write(final / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "beyond original classification"):
            self.build([final])

    def test_effective_summary_without_frozen_source_is_rejected(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        summary = json.loads((final / "summary.json").read_text())
        summary["articles"][0].update(effective_status="classified", effective_category="solid")
        write(final / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "requires its frozen routing sources"):
            self.build([final])

    def test_legacy_main_and_separate_followup_keep_distinct_classification(self):
        final, original, fallback, _ = self.routing_fixture()
        root = final.parent
        write(root / "selection.json", {"requested_pmids": ["101"]})
        summary = json.loads((final / "summary.json").read_text())
        summary["articles"] = original["records"]
        write(final / "summary.json", summary)
        child_run = root.name + "-fulltext-followup"
        child = self.fixture(child_run, "102", parent=root)
        fallback_path = root / "fallback-fulltext-v1/routing.json"
        child_classification = {"records": [{"pmid": "102", "status": "classified", "category": "solid",
                                              "original_category": "non_neoplastic",
                                              "classification_source": str(fallback_path)}],
                                "solid_pmids": ["102"], "scope": "fulltext_fallback", "parent_source_run": root.name}
        write(child.parent / "classification/selection.json", child_classification)
        write(child.parent / "batch-manifest.json", {"pmids": fallback["continue_pmids"],
              "parent_source_run": root.name, "fallback_routing_sha256": file_sha(fallback_path)})
        child_summary = json.loads((child / "summary.json").read_text())
        child_summary["articles"] = child_classification["records"]
        write(child / "summary.json", child_summary)
        record, _ = self.build([final, child])
        self.assertEqual(record["sample_count"], 3)
        self.assertIn("元数据原始分类：实体瘤 1 篇，其他已分类 2 篇", record["dataset"]["notes"][0])
        self.assertIn("独立全文补判分类：实体瘤 1 篇", record["dataset"]["notes"][1])
        self.assertIn("不能用旧标签覆盖补判", record["dataset"]["notes"][2])
        fallback["records"][1]["fallback_category"] = "non_neoplastic"
        write(fallback_path, fallback)
        with self.assertRaisesRegex(ValueError, "Follow-up classification provenance differs"):
            self.build([final, child])

    def test_real_registration_path_is_idempotent_with_isolated_synthetic_directory(self):
        final = self.fixture("ten-paper-closure-20261102", "101")
        output = self.root / "report.json"
        directory = self.root / "synthetic-research-data"
        argv = ["publish_literature_batch", "--final", str(final), "--dictionary", str(self.dictionary_path),
                "--id", "synthetic-report", "--title", "Synthetic report", "--output", str(output), "--register"]
        with patch("sys.argv", argv), patch("scripts.publish_literature_batch.register_record",
                                          side_effect=lambda record: register_record(record, directory=directory)):
            main()
            main()
        self.assertEqual(read_record("synthetic-report", directory=directory), json.loads(output.read_text()))


if __name__ == "__main__":
    unittest.main()
