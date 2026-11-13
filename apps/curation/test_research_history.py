import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.curation import research_history as history


def example():
    return {
        "schema_version": history.SCHEMA,
        "id": "test-run-v1",
        "title": "Synthetic experiment",
        "date": "2026-10-22",
        "stage": "classification",
        "status": "evaluated",
        "sample_count": 2,
        "summary": "Test fixture only",
        "task": {},
        "dataset": {},
        "reference": {},
        "identity": {
            "task_version": "fixture-task-v1",
            "sample_versions": ["sample-v1"],
            "method_versions": ["method-v1"],
            "reference_versions": ["reference-v1"],
            "completeness": "frozen",
        },
        "methods": [{"metrics": [{"label": "agreement", "numerator": 1, "denominator": 2}]}],
        "findings": [],
        "next_steps": [],
        "provenance": [],
    }


class ResearchHistoryTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_append_reuse_and_conflict_preserve_existing_record(self):
        record = example()
        first = history.register_record(record, self.directory)
        self.assertEqual(first["status"], "created")
        self.assertEqual(history.register_record(record, self.directory)["status"], "reused")
        original = (self.directory / "test-run-v1.json").read_bytes()
        record["findings"].append("Revised conclusion")
        with self.assertRaisesMessage(ValueError, "new version"):
            history.register_record(record, self.directory)
        self.assertEqual(original, (self.directory / "test-run-v1.json").read_bytes())
        record["id"] = "test-run-v2"
        history.register_record(record, self.directory)
        self.assertEqual(len(history.find_identity(record["identity"], self.directory)["matches"]), 2)

    def test_public_summary_fallback_prefers_complete_local_record(self):
        local = self.directory / "local"
        public = self.directory / "public"
        complete = example()
        complete["findings"] = ["Detailed local result"]
        summary = copy.deepcopy(complete)
        summary["findings"] = ["Public summary"]
        with patch.object(history, "DIRECTORY", local), patch.object(history, "PUBLIC_DIRECTORY", public):
            history.register_record(summary, public)
            self.assertEqual(history.read_record("test-run-v1", local)["findings"], ["Public summary"])
            self.assertEqual(len(history.records_index(local)["experiments"]), 1)
            history.register_record(complete, local)
            self.assertEqual(history.read_record("test-run-v1", local)["findings"], ["Detailed local result"])
            self.assertEqual(len(history.records_index(local)["experiments"]), 1)
            self.assertEqual(len(history.find_identity(complete["identity"], local)["matches"]), 1)

    def test_identity_changes_when_samples_method_reference_or_task_change(self):
        record = example()
        history.register_record(record, self.directory)
        for field in ("task_version", "sample_versions", "method_versions", "reference_versions"):
            identity = copy.deepcopy(record["identity"])
            identity[field] = "new-task" if field == "task_version" else ["new-version"]
            self.assertEqual(history.find_identity(identity, self.directory)["matches"], [])

    def test_latest_experiment_compares_timestamps_in_different_timezones(self):
        old = example()
        old.update(id="older-local", date="2026-11-01T12:15:24+08:00", task_kind="three_class")
        new = example()
        new.update(id="newer-utc", date="2026-11-01T06:31:02+00:00", task_kind="entity_resolution", stage="entities")
        for record in (old, new):
            history.register_record(record, self.directory)
        index = history.records_index(self.directory)
        self.assertEqual(index["experiments"][0]["id"], "newer-utc")
        self.assertEqual(index["default_experiment"], "newer-utc")
        self.assertEqual(index["current_task"]["id"], "local-entity-resolution-v1")

    def test_detects_modified_result_and_rejects_path_traversal(self):
        history.register_record(example(), self.directory)
        path = self.directory / "test-run-v1.json"
        content = json.loads(path.read_text())
        content["methods"][0]["metrics"][0]["numerator"] = 2
        path.write_text(json.dumps(content))
        with self.assertRaisesMessage(ValueError, "checksum"):
            history.read_record("test-run-v1", self.directory)
        for record_id in ("../test-run-v1", "/etc/passwd", "test/run"):
            with self.assertRaises(FileNotFoundError):
                history.read_record(record_id, self.directory)

    def test_latest_target_preview_is_not_presented_as_a_classification_score(self):
        record = example()
        record.update(task_kind="target_compatibility", stage="knowledge", status="unscored",
                      summary="12 articles; compatibility preview, no accuracy score")
        history.register_record(record, self.directory)
        index = history.records_index(self.directory)
        self.assertEqual(index["default_experiment"], record["id"])
        self.assertEqual(index["current_task"]["status"], "unscored")
        self.assertEqual(index["current_task"]["categories"], ["批量提取", "实体归一", "两层出处", "目标兼容"])
        self.assertNotIn("三分类试验已完成", index["current_task"]["note"])

    def test_requires_nonzero_valid_metric_denominator(self):
        for numerator, denominator in ((0, 0), (3, 2), (-1, 2), (True, 2)):
            record = example()
            record["methods"][0]["metrics"][0].update(numerator=numerator, denominator=denominator)
            with self.assertRaisesMessage(ValueError, "denominator"):
                history.register_record(record, self.directory)

    def test_read_only_endpoints_work_without_database(self):
        response = self.client.get("/api/v1/curation/research/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], history.SCHEMA)
        self.assertIn(response.json()["current_task"]["categories"],
                      (["实体瘤", "血液肿瘤", "非肿瘤"], ["相关", "不相关"],
                       ["实体", "关系", "条件", "两层出处"], ["核验", "发布", "检索", "两层出处"],
                       ["实体归一", "别名复用", "主癌种／亚型", "文献新增"],
                       ["批量提取", "实体归一", "两层出处", "目标兼容"]))
        self.assertIn(response.json()["current_task"]["status"], ("planned", "evaluated", "unscored"))
        self.assertEqual(response["Cache-Control"], "no-store")
        for row in response.json()["experiments"]:
            detail = self.client.get(f"/api/v1/curation/research/{row['id']}/")
            self.assertEqual(detail.status_code, 200)
            self.assertNotIn("model_result_path", detail.content.decode())
            self.assertNotIn("/data/06_project", detail.content.decode())
        self.assertEqual(self.client.get("/api/v1/curation/research/missing/").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/curation/research/").status_code, 405)

    def test_public_artifact_references_preserve_frozen_results_and_identity(self):
        project = Path(history.__file__).resolve().parents[2]
        record = example()
        record["provenance"] = [
            {"artifact": str(project / "var/experiments/test/result.json"), "sha256": "unchanged"},
            {"artifact": "/appdata/private-location/result.json", "sha256": "also-unchanged"},
        ]
        record = history.prepare_record(record)
        original = copy.deepcopy(record)
        public = history.public_record(record)
        self.assertEqual(public["provenance"][0]["artifact"], "var/experiments/test/result.json")
        self.assertEqual(public["provenance"][1]["artifact"], "result.json")
        self.assertEqual(public["provenance"][0]["sha256"], "unchanged")
        self.assertEqual(record, original)
        self.assertEqual(public["fingerprint"], record["fingerprint"])
        self.assertEqual(public["methods"], record["methods"])
        self.assertEqual(history.prepare_record(public), public)

    def test_raw_label_diagnostic_does_not_promote_rejected_output(self):
        record = history.read_record("scope-pilot-20261022")
        self.assertEqual(len(record["cases"]), 30)
        case = next(row for row in record["cases"] if row["pmid"] == "42872560")
        self.assertEqual(case["model_raw_relevance"], case["reference_relevance"])
        self.assertEqual(case["model_raw_relevance"], "non_neoplastic")
        self.assertEqual(case["model_status"], "invalid_output")
        self.assertIsNone(case["model_relevance"])
        self.assertEqual(case["model_relevance_outcome"], "failed")

    def test_passage_stage_uses_paragraph_units_and_preserves_historical_stages(self):
        record = example()
        record.update(id="passage-test", task_kind="passage_screening", stage="passages",
                      sample_count=282, sample_unit="段", dataset={"document_count": 5})
        history.register_record(record, self.directory)
        index = history.records_index(self.directory)
        self.assertEqual(index["default_experiment"], "passage-test")
        self.assertEqual(index["experiments"][0]["sample_unit"], "段")
        self.assertIn("5 篇文献、282 个已解析段落", index["current_task"]["note"])
        self.assertEqual(next(s for s in index["stages"] if s["id"] == "passages")["status"], "evaluated")
        self.assertEqual(next(s for s in history.STAGES if s["id"] == "passages")["status"], "unscored")

    def test_knowledge_stage_is_latest_on_same_day_and_keeps_previous_stage_results(self):
        for record_id, task_kind, stage, count, unit in (
            ("three-class-test", "three_class", "classification", 100, "篇"),
            ("treatment-passage-test", "passage_screening", "passages", 282, "段"),
            ("knowledge-candidates-test", "knowledge_candidates", "entities", 15, "条"),
        ):
            record = example()
            record.update(id=record_id, task_kind=task_kind, stage=stage, sample_count=count,
                          sample_unit=unit, dataset={"document_count": 5})
            history.register_record(record, self.directory)
        index = history.records_index(self.directory)
        self.assertEqual(index["default_experiment"], "knowledge-candidates-test")
        self.assertEqual(index["experiments"][0]["sample_unit"], "条")
        self.assertIn("5 篇文献、15 条知识候选", index["current_task"]["note"])
        self.assertEqual(index["current_task"]["categories"], ["实体", "关系", "条件", "两层出处"])
        by_stage = {stage["id"]: stage for stage in index["stages"]}
        for stage in ("classification", "passages", "entities"):
            self.assertEqual(by_stage[stage]["status"], "evaluated")
        self.assertEqual(by_stage["knowledge"]["status"], "not_run")
        self.assertEqual(next(s for s in history.STAGES if s["id"] == "entities")["status"], "not_run")

    def test_corrupt_record_reports_controlled_unavailable_without_file_details(self):
        with patch.object(history, "records_index", side_effect=ValueError("private/path")):
            response = self.client.get("/api/v1/curation/research/")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private/path", response.content.decode())
