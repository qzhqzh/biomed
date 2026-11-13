import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.curation.decision import digest
from scripts.prepare_knowledge_intake import freeze
from scripts.prepare_minimal_e2e import freeze_snapshot_sources
from scripts.run_minimal_pipeline import (
    CLINICAL_RELATION_PROFILE,
    ENTITY_DEFINITION,
    LEGACY_RELATION_PROFILE,
    check_packets,
    checked_entity_results,
    persist_delivery,
    prepare_entities,
    prepare_relations,
    prepare_screening_profile,
    project_entity_packet,
    relation_configuration,
    relation_inputs,
    require_model_completion,
    run_pipeline,
    run_step,
    screen_pipeline,
    select_batch,
)
from scripts.test_prepare_minimal_e2e import snapshot_fixture


class MinimalTwoStageTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "id": "E-123-0001", "article_pmid": "123", "anchor_id": "p1",
            "article": {"pmid": "123", "title": "Source title", "version_id": "v1", "source_sha256": "a" * 64},
            "passages": [{"id": "p1", "alias": "P1", "pmid": "123", "version_id": "v1",
                          "source_sha256": "a" * 64, "parser_version": "parser-v1", "locator": "body/p:1",
                          "ordinal": 1, "role": "anchor", "text": "Drug A benefits Cancer X."}],
        }
        self.result = {"status": "classified", "payload": {"entities": [
            {"type": "drug", "text": "Drug A"}, {"type": "disease", "text": "Cancer X"}]}}

    def test_prepare_reuses_original_definition_and_never_copies_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            packet = copy.deepcopy(self.packet)
            packet["entities"] = [{"text": "STALE ANSWER"}]
            source = root / "source.json"
            freeze(source, {"records": [packet]})
            plan = prepare_entities(source, root)
            actual = json.loads((root / "entities/definition.json").read_text())
            self.assertEqual(actual, json.loads(ENTITY_DEFINITION.read_text()))
            prompt = json.loads((root / "entities/inputs.json").read_text())["records"][0]["prompt"]
            self.assertNotIn("STALE ANSWER", prompt)
            self.assertNotIn("entities", json.loads((root / "entities/packets.json").read_text())["records"][0])
            self.assertEqual(plan, prepare_entities(source, root))
            self.assertFalse(plan["input_truncation"])

    def test_entity_inventory_uses_new_output_and_exact_source_locations(self):
        row = project_entity_packet(self.packet, self.result)
        self.assertEqual(row["entity_stage"]["execution_status"], "complete")
        self.assertEqual([e["text"] for e in row["entities"]], ["Drug A", "Cancer X"])
        for entity in row["entities"]:
            loc = entity["locations"][0]
            self.assertEqual(self.packet["passages"][0]["text"][loc["start"]:loc["end"]], entity["text"])
        self.result["payload"]["entities"].append({"type": "drug", "text": "Drug A"})
        deduped = project_entity_packet(self.packet, self.result)
        self.assertEqual(len(deduped["entities"]), 2)
        self.assertEqual(deduped["entities"][0]["raw_indices"], [0, 2])

    def test_unlocated_extra_is_kept_in_lineage_but_not_in_relation_prompt(self):
        self.result["payload"]["entities"].append({"type": "drug", "text": "Not In Source"})
        row = project_entity_packet(self.packet, self.result)
        packets, inputs, deferred = relation_inputs({"records": [row]})
        self.assertEqual(deferred, [])
        self.assertEqual(len(packets["records"][0]["entities"]), 3)
        self.assertEqual(len(packets["records"][0]["deferred_entities"]), 1)
        self.assertNotIn("Not In Source", inputs["records"][0]["prompt"])
        self.assertIn("Drug A", inputs["records"][0]["prompt"])

    def test_truncated_entities_are_not_salvaged_or_reported_as_negative(self):
        self.result["status"] = "output_truncated"
        failed = project_entity_packet(self.packet, self.result)
        self.assertEqual(failed["entities"], [])
        self.assertEqual(failed["entity_stage"]["execution_status"], "failed")
        packets, inputs, deferred = relation_inputs({"records": [failed]})
        self.assertEqual(packets["records"], [])
        self.assertEqual(inputs["records"], [])
        self.assertEqual(deferred[0]["id"], self.packet["id"])

    def test_source_versions_and_explicit_over_budget_are_rejected(self):
        wrong = copy.deepcopy(self.packet)
        wrong["passages"][0]["version_id"] = "another"
        with self.assertRaisesRegex(ValueError, "mixes article"):
            check_packets({"records": [wrong]})
        wrong = copy.deepcopy(self.packet)
        wrong["context_budget_status"] = "over_budget"
        with self.assertRaisesRegex(ValueError, "context budget"):
            check_packets({"records": [wrong]})

    def test_checkpoints_are_bound_to_prepared_inputs_and_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.json"
            freeze(source, {"records": [self.packet]})
            prepare_entities(source, root)
            data = json.loads((root / "entities/inputs.json").read_text())
            definition = json.loads((root / "entities/definition.json").read_text())
            config = {"definition": definition, "model_id": "fixture-only"}
            output = root / "entities/extract"
            freeze(output / "inputs.json", data)
            freeze(output / "manifest.json", {"config": config, "config_sha256": digest(config),
                                              "input_sha256": digest(data), "definition_sha256": digest(definition)})
            with self.assertRaisesRegex(ValueError, "incomplete"):
                checked_entity_results(root)
            result = {**self.result, "id": self.packet["id"], "input_sha256": data["records"][0]["input_sha256"],
                      "config_sha256": digest(config), "raw_output": json.dumps(self.result["payload"]),
                      "input_tokens": 10, "output_tokens": 5, "seconds": 0.1}
            path = output / f"result-{self.packet['id']}.json"
            freeze(path, result)
            self.assertEqual(len(checked_entity_results(root)[0]), 1)
            result["payload"]["entities"][0]["text"] = "Injected"
            path.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "differs from raw"):
                checked_entity_results(root)

    def test_stage_dependency_is_consumed_and_completed_work_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original, first, final = (root / name for name in ("source", "first", "final"))
            original.write_text("actual upstream")
            state = {"stages": {}}
            state_path = root / "state.json"
            calls = []

            def one():
                calls.append("one")
                first.write_text(original.read_text() + " stage-one")

            def two():
                calls.append("two")
                final.write_text(first.read_text() + " stage-two")

            run_step(state, state_path, "one", one, inputs=[original], outputs=[first])
            run_step(state, state_path, "two", two, inputs=[first], outputs=[final])
            self.assertEqual(final.read_text(), "actual upstream stage-one stage-two")
            self.assertEqual(run_step(state, state_path, "one", one, inputs=[original], outputs=[first]), "reused")
            self.assertEqual(calls, ["one", "two"])
            original.write_text("changed input")
            with self.assertRaisesRegex(ValueError, "dependency changed"):
                run_step(state, state_path, "one", one, inputs=[original], outputs=[first])
            self.assertEqual(calls, ["one", "two"])
            self.assertEqual(json.loads(state_path.read_text())["status"], "failed")

    def test_subprocess_failure_or_missing_output_never_marks_stage_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.write_text("input")
            state, state_path = {"stages": {}}, root / "state.json"

            def failed():
                raise RuntimeError("Subprocess exited 7")

            with self.assertRaisesRegex(RuntimeError, "exited 7"):
                run_step(state, state_path, "model", failed, inputs=[source], outputs=[root / "missing"])
            self.assertEqual(state["stages"]["model"]["status"], "failed")
            with self.assertRaises(FileNotFoundError):
                run_step(state, state_path, "model", lambda: None, inputs=[source], outputs=[root / "missing"])
            self.assertEqual(state["status"], "failed")

    def test_explicit_batch_limit_preserves_all_pending_and_original_order(self):
        packets = []
        for pmid in ("123", "456"):
            for ordinal in (4, 1, 3, 2):
                p = copy.deepcopy(self.packet)
                p["id"] = f"E-{pmid}-{ordinal}"
                p["article_pmid"] = pmid
                p["passages"][0]["ordinal"] = ordinal
                packets.append(p)
        selected, plan = select_batch({"records": packets}, 3)
        self.assertEqual([p["id"] for p in selected["records"]], ["E-123-1", "E-123-2", "E-123-3", "E-456-1", "E-456-2", "E-456-3"])
        self.assertEqual(plan["remaining_packets"], 2)
        self.assertEqual(plan["pending_packet_ids"], ["E-123-4", "E-456-4"])
        all_selected, unlimited = select_batch({"records": packets})
        self.assertEqual(len(all_selected["records"]), 8)
        self.assertEqual(unlimited["remaining_packets"], 0)

    def test_article_selection_processes_all_its_positives_and_records_other_work(self):
        packets = []
        for pmid in ("123", "456"):
            for ordinal in (4, 1, 3, 2):
                p = copy.deepcopy(self.packet)
                p.update(id=f"E-{pmid}-{ordinal}", article_pmid=pmid)
                p["passages"][0]["ordinal"] = ordinal
                packets.append(p)
        selected, plan = select_batch({"records": packets}, pmids=["123"])
        self.assertEqual([p["id"] for p in selected["records"]], [f"E-123-{i}" for i in range(1, 5)])
        self.assertEqual(plan["remaining_packets"], 4)
        self.assertEqual(plan["pending_selected_article_packets"], 0)
        with self.assertRaisesRegex(ValueError, "no eligible positive"):
            select_batch({"records": packets}, pmids=["unknown"])

    def test_over_budget_packet_is_pending_without_blocking_another_article(self):
        large = copy.deepcopy(self.packet)
        large["context_budget_status"] = "over_budget"
        ready = copy.deepcopy(self.packet)
        ready["id"], ready["article_pmid"] = "E-456-0001", "456"
        selected, plan = select_batch({"records": [large, ready]}, pmids=["123", "456", "789"], allow_absent=True)
        self.assertEqual([row["id"] for row in selected["records"]], ["E-456-0001"])
        self.assertEqual(plan["pending_reasons"], {"E-123-0001": "context_characters_over_budget"})

    def test_token_budget_failure_is_recorded_and_other_results_can_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            definition = {"version": "fixture", "system_prompt": "fixture"}
            rows = []
            for identity in ("too-long", "fits"):
                row = {"id": identity, "prompt": identity}
                rows.append({**row, "input_sha256": digest(row)})
            dataset = {"records": rows}
            config = {"definition": definition, "max_input_tokens": 4096}
            output = root / "extract"
            freeze(root / "inputs.json", dataset)
            freeze(root / "definition.json", definition)
            freeze(output / "inputs.json", dataset)
            freeze(output / "manifest.json", {"input_sha256": digest(dataset), "definition_sha256": digest(definition),
                                              "config": config, "config_sha256": digest(config)})
            freeze(output / "summary.json", {"state": "completed"})
            for index, row in enumerate(rows):
                result = {"id": row["id"], "input_sha256": row["input_sha256"], "config_sha256": digest(config),
                          "status": "input_too_long" if not index else "classified", "payload": None if not index else {},
                          "raw_output": "" if not index else "{}", "input_tokens": 5000 if not index else 20,
                          "output_tokens": 0 if not index else 2, "seconds": 0.01}
                freeze(output / f"result-{row['id']}.json", result)
            with self.assertRaisesRegex(RuntimeError, "budget exceeded"):
                require_model_completion(output, root / "inputs.json", root / "definition.json")
            results = require_model_completion(output, root / "inputs.json", root / "definition.json", allow_input_too_long=True)
            self.assertEqual(results["fits"]["status"], "classified")
            self.assertEqual(json.loads((root / "input-budget-failures.json").read_text())["records"][0]["id"], "too-long")

    def test_no_positive_and_missing_fulltext_complete_without_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = snapshot_fixture(root)
            batch = root / "batch"
            freeze_snapshot_sources(batch, [snapshot], ["123", "456"], root / "experiments")
            source = json.loads((batch / "screening/inputs.json").read_text())

            def fake_screen(command, logfile):
                self.assertEqual(command[1], "scripts/benchmark_passage_screening.py")
                output = batch / "screening/qwen35-9b"
                config = {"fixture": True}
                freeze(output / "inputs.json", source)
                freeze(output / "manifest.json", {"input_sha256": digest(source), "config": config, "config_sha256": digest(config)})
                freeze(output / "summary.json", {"state": "completed"})
                for row in source["records"]:
                    result = {key: row[key] for key in ("id", "pmid", "version_id", "source_sha256", "parser_version", "ordinal", "input_sha256")}
                    result.update(config_sha256=digest(config), status="classified", relevant=False,
                                  raw_output='{"relevant":false}', input_tokens=10, output_tokens=4, seconds=0.1)
                    freeze(output / f"result-{row['id']}.json", result)

            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=fake_screen):
                self.assertEqual(screen_pipeline(batch)["processed"], 1)
            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=AssertionError("unexpected model call")):
                summary = run_pipeline(batch)
                self.assertEqual(summary, run_pipeline(batch))
            self.assertEqual(summary["state"], "batch_completed_with_deferred")
            self.assertEqual([row["status"] for row in summary["article_outcomes"]], ["no_eligible_positive", "fulltext_missing"])
            self.assertEqual(summary["delivered_records"], 0)
            self.assertFalse(summary["database_written"])

    def test_all_sources_missing_skips_screen_and_model_stages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = snapshot_fixture(root)
            batch = root / "batch"
            freeze_snapshot_sources(batch, [snapshot], ["456", "999"], root / "experiments")
            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=AssertionError("unexpected model call")):
                self.assertEqual(screen_pipeline(batch)["state"], "no_screenable_fulltext")
                summary = run_pipeline(batch)
            self.assertEqual([row["status"] for row in summary["article_outcomes"]], ["fulltext_missing", "not_found_in_snapshots"])
            self.assertEqual(summary["state"], "batch_completed_with_deferred")

    def test_legacy_manifest_inherits_old_prompt_and_rejects_mid_batch_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            freeze(root / "pipeline-manifest.json", {"version": "minimal-two-stage-v3"})
            self.assertEqual(relation_configuration(root), LEGACY_RELATION_PROFILE)
            with self.assertRaisesRegex(ValueError, "new pipeline version"):
                relation_configuration(root, CLINICAL_RELATION_PROFILE)
            with self.assertRaisesRegex(ValueError, "Unknown relation profile"):
                relation_configuration(root, "unknown")

    def test_clinical_profile_is_frozen_and_resumes_without_explicit_option(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = snapshot_fixture(root)
            batch = root / "batch"
            freeze_snapshot_sources(batch, [snapshot], ["999"], root / "experiments")
            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=AssertionError("unexpected model call")):
                original = run_pipeline(batch, relation_profile=CLINICAL_RELATION_PROFILE)
                manifest_before = (batch / "pipeline-manifest.json").read_bytes()
                self.assertEqual(run_pipeline(batch), original)
                self.assertEqual((batch / "pipeline-manifest.json").read_bytes(), manifest_before)
                manifest = json.loads(manifest_before)
                self.assertEqual(manifest["relation_profile"], CLINICAL_RELATION_PROFILE)
                self.assertIn("relation_base_definition_sha256", manifest)
                resume = json.loads((batch / "pipeline-state.json").read_text())["resume_command"]
                self.assertIn(CLINICAL_RELATION_PROFILE, resume)
                with self.assertRaisesRegex(ValueError, "new pipeline version"):
                    run_pipeline(batch, relation_profile=LEGACY_RELATION_PROFILE)

    def test_prepared_relations_use_clinical_prompt_without_changing_entity_inputs(self):
        from apps.curation.knowledge_relation_clinical_prompt import DEFINITION

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stages = root / "stages"
            freeze(root / "pipeline-manifest.json", {"relation_profile": CLINICAL_RELATION_PROFILE})
            packet = project_entity_packet(self.packet, self.result)
            dataset = {"records": [packet]}
            freeze(stages / "entities/assembled-packets.json", dataset)
            freeze(stages / "entities/assembly.json", {"packets_sha256": digest(dataset), "model_config_sha256": "fixture"})
            with patch("scripts.run_minimal_pipeline.checked_entity_results", return_value=({}, {"config_sha256": "fixture"})):
                plan = prepare_relations(stages)
                self.assertEqual(plan, prepare_relations(stages))
            definition = json.loads((stages / "relations/definition.json").read_text())
            self.assertEqual(definition, DEFINITION)
            self.assertEqual(plan["prepared_records"], 1)
            self.assertEqual(json.loads((stages / "relations/all-packets.json").read_text()), dataset)
            self.assertIn("clinical_prompt.py", plan["definition_source"])

    def test_diagnostic_screening_freezes_sidecar_and_cannot_relabel_legacy_predictions(self):
        original = {"version": "solid-treatment-passage-screening-v1",
                    "prompt": "Decide whether the CENTER paragraph contains an extractable therapeutic evidence statement about a solid tumor.",
                    "labels": [True, False, None]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            freeze(root / "screening/definition.json", original)
            freeze(root / "screening/inputs.json", {"records": []})
            initial = (root / "screening/definition.json").read_bytes()
            active = prepare_screening_profile(root, CLINICAL_RELATION_PROFILE)
            self.assertEqual(active, root / "screening/clinical-profile")
            self.assertEqual(prepare_screening_profile(root), active)
            definition = json.loads((active / "definition.json").read_text())
            self.assertEqual(definition["labels"], [True, False, None])
            self.assertIn("actual patient/case or tumor pathology", definition["prompt"])
            self.assertIn("generic molecular/diagnostic background", definition["prompt"])
            self.assertIn("without a drug", definition["prompt"])
            self.assertEqual((root / "screening/definition.json").read_bytes(), initial)
            self.assertEqual((active / "inputs.json").read_bytes(), (root / "screening/inputs.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "new pipeline version"):
                prepare_screening_profile(root, LEGACY_RELATION_PROFILE)

            legacy = root / "legacy"
            freeze(legacy / "screening/definition.json", original)
            freeze(legacy / "screening/inputs.json", {"records": []})
            freeze(legacy / "screening/qwen35-9b/manifest.json", {"config": {"definition": original}})
            checkpoint = legacy / "screening/qwen35-9b/result-existing.json"
            checkpoint.write_text('{"relevant": false}')
            with self.assertRaisesRegex(ValueError, "preserve existing checkpoints"):
                prepare_screening_profile(legacy, CLINICAL_RELATION_PROFILE)
            self.assertEqual(checkpoint.read_text(), '{"relevant": false}')
            self.assertFalse((legacy / "screening/clinical-profile").exists())
            self.assertEqual(prepare_screening_profile(legacy), legacy / "screening")

    def test_screen_runner_uses_clinical_sidecar_with_same_9b_and_original_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = snapshot_fixture(root)
            definition_path = root / "experiments/evidence-passages-20261023/definition.json"
            definition_path.write_text(json.dumps({"version": "solid-treatment-passage-screening-v1", "prompt": "test"}))
            batch = root / "batch"
            freeze_snapshot_sources(batch, [snapshot], ["123"], root / "experiments")
            source = json.loads((batch / "screening/inputs.json").read_text())

            def fake_screen(command, logfile):
                self.assertEqual(command[command.index("--base") + 1], str(batch / "screening/clinical-profile"))
                self.assertEqual(command[command.index("--model-id") + 1], "Qwen/Qwen3.5-9B")
                definition = json.loads((batch / "screening/clinical-profile/definition.json").read_text())
                config = {"definition": definition}
                output = batch / "screening/qwen35-9b"
                freeze(output / "inputs.json", source)
                freeze(output / "manifest.json", {"input_sha256": digest(source), "config": config, "config_sha256": digest(config)})
                freeze(output / "summary.json", {"state": "completed"})
                for row in source["records"]:
                    result = {key: row[key] for key in ("id", "pmid", "version_id", "source_sha256", "parser_version", "ordinal", "input_sha256")}
                    result.update(config_sha256=digest(config), status="classified", relevant=False,
                                  raw_output='{"relevant":false}', input_tokens=10, output_tokens=4, seconds=0.1)
                    freeze(output / f"result-{row['id']}.json", result)

            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=fake_screen):
                self.assertEqual(screen_pipeline(batch, relation_profile=CLINICAL_RELATION_PROFILE)["processed"], 1)
                self.assertEqual(screen_pipeline(batch)["processed"], 1)
            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=AssertionError("unexpected model call")):
                result = run_pipeline(batch)
                self.assertEqual(result["delivered_records"], 0)
            manifest = json.loads((batch / "pipeline-manifest.json").read_text())
            self.assertEqual(manifest["relation_profile"], CLINICAL_RELATION_PROFILE)

    def test_one_token_budget_failure_does_not_stop_other_positive_packet(self):
        from scripts.minimal_knowledge_pilot import assemble

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot_path = snapshot_fixture(root)
            envelope = json.loads(snapshot_path.read_text())
            snapshot = envelope["snapshot"]
            document = snapshot["documents"][0]
            raw = (b'<article><front><article-meta><article-id pub-id-type="pmid">123</article-id></article-meta></front>'
                   b'<body><p>Drug A benefits Cancer X.</p><p>Drug A improved survival.</p></body></article>')
            checksum = hashlib.sha256(raw).hexdigest()
            blob = root / "library/blobs" / checksum[:2] / checksum
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(raw)
            document["versions"][0]["sha256"] = checksum
            document["passages"] = [
                {"id": f"p{index}", "version_id": "v1", "parser_version": "jats-v1", "locator": f"body/p:{index}",
                 "ordinal": index - 1, "text": text, "kind": "fulltext"}
                for index, text in enumerate(("Drug A benefits Cancer X.", "Drug A improved survival."), 1)
            ]
            document["passages_sha256"] = digest(document["passages"])
            snapshot_path.write_text(json.dumps({"snapshot": snapshot, "sha256": digest(snapshot)}))
            batch = root / "batch"
            freeze_snapshot_sources(batch, [snapshot_path], ["123"], root / "experiments")

            def fake_execute(command, logfile, env=None):
                script = command[1]
                if script.endswith("benchmark_passage_screening.py"):
                    source = json.loads((batch / "screening/inputs.json").read_text())
                    output = batch / "screening/qwen35-9b"
                    config = {"fixture": "screen"}
                    freeze(output / "inputs.json", source)
                    freeze(output / "manifest.json", {"input_sha256": digest(source), "config": config, "config_sha256": digest(config)})
                    freeze(output / "summary.json", {"state": "completed"})
                    for row in source["records"]:
                        result = {key: row[key] for key in ("id", "pmid", "version_id", "source_sha256", "parser_version", "ordinal", "input_sha256")}
                        result.update(config_sha256=digest(config), status="classified", relevant=True,
                                      raw_output='{"relevant":true}', input_tokens=10, output_tokens=4, seconds=0.1)
                        freeze(output / f"result-{row['id']}.json", result)
                elif script.endswith(("benchmark_knowledge_candidates.py", "benchmark_bonsai_stage.py")):
                    is_entities = script.endswith("benchmark_knowledge_candidates.py")
                    stage = batch / ("stages/entities" if is_entities else "stages/relations")
                    source = json.loads((stage / "inputs.json").read_text())
                    definition = json.loads((stage / "definition.json").read_text())
                    config = {"definition": definition, "max_input_tokens": 4096}
                    output = stage / "extract"
                    freeze(output / "inputs.json", source)
                    freeze(output / "manifest.json", {"input_sha256": digest(source), "definition_sha256": digest(definition),
                                                      "config": config, "config_sha256": digest(config)})
                    freeze(output / "summary.json", {"state": "completed"})
                    packets = json.loads((stage / "packets.json").read_text())["records"]
                    for index, (row, packet) in enumerate(zip(source["records"], packets, strict=True)):
                        long = is_entities and index == 0
                        if is_entities:
                            payload = {"entities": [{"type": "drug", "text": "Drug A"}]}
                        else:
                            anchor = next(p["alias"] for p in packet["passages"] if p["role"] == "anchor")
                            payload = {"fact": {"relation": "treatment_outcome", "conclusion": "Drug A improved survival.",
                                                "population": None, "treatment": "Drug A", "comparator": None,
                                                "conditions": [], "scope": "fixture", "entity_ids": [packet["entities"][0]["id"]],
                                                "evidence": [anchor]}, "reason": None}
                            second = copy.deepcopy(payload["fact"])
                            second.update(relation="other", conclusion="A separate fixture observation")
                            payload = {"facts": [payload["fact"], second], "reason": None}
                        result = {"id": row["id"], "input_sha256": row["input_sha256"], "config_sha256": digest(config),
                                  "status": "input_too_long" if long else "classified", "payload": None if long else payload,
                                  "raw_output": "" if long else json.dumps(payload), "input_tokens": 5000 if long else 20,
                                  "output_tokens": 0 if long else 10, "seconds": 0.1}
                        freeze(output / f"result-{row['id']}.json", result)
                else:
                    self.assertTrue(script.endswith("minimal_knowledge_pilot.py"))
                    assemble(SimpleNamespace(root=batch / "stages/relations", mode="inventory", source_run=batch.name, output=None))

            with patch("scripts.run_minimal_pipeline.execute_command", side_effect=fake_execute) as execute, \
                 patch("scripts.minimal_knowledge_pilot.standard_linker", return_value=None), \
                 patch("scripts.pubtator_reference.optional_reference", return_value={
                     "state": "completed", "candidates": 3, "local_models_skipped": False,
                 }) as reference:
                screen_pipeline(batch)
                summary = run_pipeline(batch)
                scripts = [call.args[0][1] for call in execute.call_args_list]
                self.assertIn("scripts/benchmark_knowledge_candidates.py", scripts)
                self.assertIn("scripts/benchmark_bonsai_stage.py", scripts)
                reference.assert_called_once()
                original_bundle = (batch / "delivery/bundle.json").read_bytes()
                reference.return_value = {"state": "unavailable", "local_models_skipped": False}
                resumed = run_pipeline(batch)
                self.assertEqual(resumed["state"], summary["state"])
                self.assertEqual(resumed["pubtator"]["state"], "unavailable")
                self.assertEqual((batch / "delivery/bundle.json").read_bytes(), original_bundle)
            self.assertEqual(summary["batch_packets"], 2)
            self.assertEqual(summary["entity_stage_failed"], 1)
            self.assertEqual(summary["relation_stage_inputs"], 1)
            self.assertEqual(summary["article_outcomes"][0]["status"], "deferred_or_partial")
            self.assertEqual(summary["state"], "batch_completed_with_deferred")
            bundle = json.loads((batch / "delivery/bundle.json").read_text())
            self.assertEqual(len(bundle["records"]), 3)
            self.assertEqual([r["assessment"]["execution"]["status"] for r in bundle["records"]], ["failed", "complete", "complete"])
            self.assertEqual(bundle["records"][1]["provenance"]["source_packet_id"], bundle["records"][2]["provenance"]["source_packet_id"])

    def test_local_persistence_requires_actual_success_and_registry_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            freeze(root / "delivery/bundle.json", {"source_run": "test", "records": []})
            receipt_path = root / "delivery/import-receipt.json"
            with patch("scripts.run_minimal_pipeline.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=1, stderr=b"temporary database failure")
                with self.assertRaisesRegex(RuntimeError, "Local knowledge import failed"):
                    persist_delivery(root)
                self.assertFalse(receipt_path.exists())
                run.return_value = SimpleNamespace(returncode=0, stdout=b'{"created": 1}')
                with self.assertRaisesRegex(ValueError, "entity-registry"):
                    persist_delivery(root)
                self.assertFalse(receipt_path.exists())
                run.return_value = SimpleNamespace(returncode=0, stdout=b'{"created": 1, "entity_registry": {"resolved": 2}}')
                persist_delivery(root)
                self.assertFalse(json.loads(receipt_path.read_text())["target_written"])
                self.assertIn("import_knowledge_candidates", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
