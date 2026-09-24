"""HTTP 层：三类错误状态码可区分；重复提交结果一致且指标不重复。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import log_case
from dqv.service import app, engine


@pytest.fixture(autouse=True)
def _fresh_engine():
    engine._slots.clear()
    engine.metrics._snapshots.clear()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


CONTRACT = {"name": "orders", "fields": [
    {"name": "id", "type": "integer", "constraints": {"unique": True, "min_value": 1}},
    {"name": "ts", "type": "timestamp"}], "freshness_max_age_seconds": 100}


def test_rejection_status_codes_are_distinguishable(client):
    cases = [
        ({"contract": {"name": "x", "fields": []}}, 422, "malformed_contract"),
        ({"contract": {"name": "x", "fields": [
            {"name": "a", "type": "integer",
             "constraints": {"min_value": 9, "max_value": 1}}]}},
         422, "conflicting_constraints"),
        ({"contract": {"name": "x"}}, 422, "missing_field"),
    ]
    for body, code, reason in cases:
        log_case(f"http-{reason}", body, f"HTTP {code} reason={reason}",
                 "ContractRejectedError 处理器映射")
        r = client.post("/contracts/validate", json=body)
        assert r.status_code == code
        assert r.json()["category"] == "contract_rejected"
        assert r.json()["reason"] == reason


def test_data_violation_is_explained_result_not_5xx(client):
    body = {"contract": CONTRACT,
            "records": [{"id": "bad", "ts": 1000}, {"id": 1, "ts": 1000}],
            "idempotency_key": "r1", "now_epoch": 1050}
    r = client.post("/validations", json=body)
    log_case("http-data-violation", body,
             "HTTP 200 passed=false，violations 含 type_mismatch",
             r.json()["violations"])
    assert r.status_code == 200 and r.json()["passed"] is False
    codes = {v["code"] for v in r.json()["violations"]}
    assert "type_mismatch" in codes


def test_duplicate_submission_consistent_and_metrics_once(client):
    body = {"contract": CONTRACT, "records": [{"id": 1, "ts": 1000}],
            "idempotency_key": "dup", "now_epoch": 1050}
    r1 = client.post("/validations", json=body).json()
    count_after_first = client.get("/contracts/orders/metrics").json()["count"]
    r2 = client.post("/validations", json=body).json()
    count_after_second = client.get("/contracts/orders/metrics").json()["count"]
    log_case("http-duplicate", body,
             "两次 passed/violations 一致；指标快照数不变",
             {"first": count_after_first, "second": count_after_second,
              "passed1": r1["passed"], "passed2": r2["passed"]})
    assert r1["passed"] == r2["passed"]
    assert r1["violations"] == r2["violations"]
    assert count_after_first == count_after_second == 4


def test_traceback_via_batch_and_metrics(client):
    body = {"contract": CONTRACT, "records": [{"id": 1, "ts": 1000}],
            "idempotency_key": "trace-1", "now_epoch": 1050}
    client.post("/validations", json=body)
    batch = client.get("/batches/trace-1")
    metrics = client.get("/contracts/orders/metrics").json()["snapshots"]
    log_case("http-trace", "batch=trace-1",
             "批次结果与指标快照都带 contract_name/version/batch_id",
             {"batch_found": batch.status_code,
              "metric_batches": {s["batch_id"] for s in metrics}})
    assert batch.status_code == 200
    assert all(s["batch_id"] == "trace-1" for s in metrics)
    assert all(s["reason"] for s in metrics)
