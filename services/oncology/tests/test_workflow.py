import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oncology_workbench.main import create_app

EXAMPLE = "虚构病例：肺腺癌，IV期，EGFR 19外显子缺失，既往用过奥希替尼。"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as result:
        yield result


def search(client, **changes):
    fields = client.post("/api/extract", json={"text": EXAMPLE}).json()["fields"]
    fields.update(changes)
    return client.post("/api/search", json={"fields": fields, "confirmed": True}).json()


def test_full_retrieval_flow_preserves_source_and_uncertainty(client):
    result = search(client)
    assert result["status"] == "matched"
    assert {row["kind"] for row in result["results"]} == {"guideline", "drug", "trial"}
    assert all(row["is_demo"] and row["source_text"] for row in result["results"])
    trial = next(row for row in result["results"] if row["kind"] == "trial")
    assert trial["match_state"] == "needs_review"
    assert any("城市" in note for note in trial["unknown"])
    assert any("招募" in note for note in trial["unknown"])
    assert any("既往治疗" in row["conflicts"][0] for row in result["excluded"])


def test_confirmation_is_required_on_server(client):
    assert client.post("/api/search", json={"fields": {}, "confirmed": False}).status_code == 422
    assert client.post("/api/search", json={"fields": {}}).status_code == 422


def test_unknown_stage_is_not_reported_as_satisfied(client):
    result = search(client, stage="")
    guideline = next(row for row in result["results"] if row["kind"] == "guideline")
    assert guideline["match_state"] == "needs_review"
    assert any("分期待核对" in note for note in guideline["unknown"])
    assert not any("分期" in note for note in guideline["matched"])


def test_conflicting_biomarker_is_excluded(client):
    result = search(client, biomarker="EGFR 阴性")
    assert result["status"] == "no_match"
    assert result["results"] == []
    assert all(row["match_state"] == "excluded" for row in result["excluded"])


def test_unknown_cancer_does_not_fall_back_to_other_cancers(client):
    result = search(client, cancer="胃癌")
    assert result["status"] == "no_match"
    assert result["results"] == []
    assert "不代表" in result["message"]
    assert search(client, cancer="")["status"] == "insufficient"


def test_negation_and_suspected_diagnosis_remain_empty(client):
    fields = client.post(
        "/api/extract",
        json={"text": "疑似肺腺癌，未检测EGFR 19外显子缺失，未用过奥希替尼，分期不详。"},
    ).json()["fields"]
    assert fields == {key: "" for key in fields}


def test_stage_iii_not_mistaken_for_i(client):
    result = client.post("/api/extract", json={"text": "虚构病例：肺腺癌，III期。"}).json()
    assert result["fields"]["stage"] == "III"


def test_city_mismatch_keeps_trial_out_of_results(client):
    result = search(client, city="北京")
    assert not any(row["kind"] == "trial" for row in result["results"])
    assert any(row["kind"] == "trial" for row in result["excluded"])


def test_issue_persists_across_app_restart_without_case_text(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result = search(client)
        response = client.post(
            "/api/issues",
            json={
                "title": "缺失项需要明确显示",
                "category": "matching",
                "observation": "使用虚构示例，城市为空。",
                "expected": "显示待核对",
                "lesson": "未知不等于满足",
                "run_id": result["run_id"],
            },
        )
        assert response.status_code == 201
        issue_id = response.json()["id"]
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/issues").json()["items"][0]["id"] == issue_id
        updated = client.patch(
            f"/api/issues/{issue_id}",
            json={"status": "resolved", "lesson": "通过来源详情核对未知项，已用虚构样例验证。"},
        )
        assert updated.json()["status"] == "resolved"
        exported = client.get("/api/issues/export").json()
        assert exported["items"][0]["lesson"].endswith("验证。")
    assert EXAMPLE.encode() not in (tmp_path / "lessons.sqlite3").read_bytes()


def test_export_is_not_silently_limited_to_screen_page(client):
    for i in range(202):
        response = client.post(
            "/api/issues",
            json={"title": f"合成问题 {i}", "category": "other", "observation": "测试导出完整性"},
        )
        assert response.status_code == 201
    assert len(client.get("/api/issues").json()["items"]) == 200
    assert len(client.get("/api/issues/export").json()["items"]) == 202


def test_cross_origin_write_is_rejected(client):
    response = client.post(
        "/api/extract", json={"text": EXAMPLE}, headers={"Origin": "https://unrelated.example"}
    )
    assert response.status_code == 403


def test_oversized_input_is_rejected(client):
    assert client.post("/api/extract", json={"text": "a" * 6001}).status_code == 422


def test_unknown_record_returns_404(client):
    result = client.patch(
        "/api/issues/00000000-0000-0000-0000-000000000000",
        json={"status": "resolved", "lesson": "尚无记录"},
    )
    assert result.status_code == 404


def test_demo_dataset_is_explicitly_labelled(client):
    rows = client.get("/api/catalog").json()["items"]
    assert len(rows) == 7
    assert all(row["id"].startswith("DEMO-") and row["is_demo"] for row in rows)
    assert "Content-Security-Policy" in client.get("/").headers
    examples = json.loads((Path(__file__).parents[1] / "data/demo/cases.json").read_text())
    assert all("虚构" in row["text"] for row in examples)


def test_internal_service_has_no_separate_frontend(client):
    assert client.get("/").json()["service"] == "biomed-evidence"
    assert client.get("/static/app.js").status_code == 404
    cases = client.get("/api/cases").json()
    assert cases["mode"] == "synthetic-demo"
    assert len(cases["items"]) == 3
    assert all("虚构病例" in case["text"] for case in cases["items"])
