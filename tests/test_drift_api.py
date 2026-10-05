"""HTTP 层：显式基线透传、基线缺失返回 404、未知配置项 422、trace 追加 drift。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.quality.api import create_app
from app.quality.service import QualityService


def _client():
    svc = QualityService()
    svc.register_contract(
        {
            "name": "c",
            "fields": [
                {"name": "id", "type": "integer", "unique": True},
            ],
        }
    )
    return TestClient(create_app(svc))


def test_explicit_baseline_and_trace_drift(case):
    c = _client()
    c.post("/contracts/c/batches/B1/validate",
           json={"batch_id": "B1", "rows": [{"id": 1}, {"id": 2}]})
    case.input("baseline", "B1")
    case.expect("B2 显式以 B1 为基线；trace.drift 追加可读结论")
    r = c.post(
        "/contracts/c/batches/B2/validate",
        json={"batch_id": "B2", "rows": [{"id": 1}, {"id": 1}],
              "baseline_batch_id": "B1"},
    )
    case.basis(r.status_code)
    assert r.status_code == 200

    trace = c.get("/contracts/c/batches/B2/trace").json()
    drift = trace["drift"]
    case.basis((drift["baseline_batch_id"], drift["baseline_mode"],
                drift["status"]))
    # 老字段保持不变，新信息只追加。
    assert set(trace.keys()) >= {"contract", "batch_id", "validation",
                                 "metrics", "drift"}
    assert trace["metrics"][0]["metric"] == "completeness"
    assert drift["baseline_batch_id"] == "B1"
    assert drift["baseline_mode"] == "explicit"
    assert drift["status"] == "regressed"
    assert all("reason" in m for m in drift["metrics"])


def test_unknown_baseline_returns_404(case):
    c = _client()
    case.input("baseline", "GHOST")
    case.expect("404 metric_not_found / baseline_batch_not_judged")
    r = c.post(
        "/contracts/c/batches/B9/validate",
        json={"batch_id": "B9", "rows": [{"id": 1}],
              "baseline_batch_id": "GHOST"},
    )
    body = r.json()["error"]
    case.basis((r.status_code, body["category"], body["reason"]))
    assert r.status_code == 404
    assert body["category"] == "metric_not_found"
    assert body["reason"] == "baseline_batch_not_judged"
    assert body["batch"] == "GHOST"


def test_first_batch_trace_drift_is_null(case):
    c = _client()
    c.post("/contracts/c/batches/B1/validate",
           json={"batch_id": "B1", "rows": [{"id": 1}]})
    case.expect("首个批次 trace.drift 为 null（不硬凑基线结论）")
    trace = c.get("/contracts/c/batches/B1/trace").json()
    case.basis(trace["drift"])
    assert trace["drift"] is None


def test_unknown_contract_key_returns_422(case):
    c = _client()
    payload = {"name": "x",
               "feilds": [{"name": "id", "type": "integer"}]}  # 顶层拼错
    case.input("payload", payload)
    case.expect("422 contract_invalid / unknown_contract_key")
    r = c.post("/contracts", json=payload)
    body = r.json()["error"]
    case.basis((r.status_code, body["reason"], body["field"]))
    assert r.status_code == 422
    assert body["category"] == "contract_invalid"
    assert body["reason"] == "unknown_contract_key"
    assert body["field"] == "feilds"


def test_unknown_field_key_returns_422(case):
    c = _client()
    payload = {"name": "x",
               "fields": [{"name": "id", "type": "integer", "uniqe": True}]}
    case.input("payload", payload)
    case.expect("422 unknown_field_key，定位到 uniqe")
    r = c.post("/contracts", json=payload)
    body = r.json()["error"]
    case.basis((r.status_code, body["reason"], body["field"]))
    assert r.status_code == 422
    assert body["reason"] == "unknown_field_key"
    assert body["field"] == "uniqe"
