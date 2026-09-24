"""HTTP 层错误分类 -> 状态码映射与幂等接口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.quality.api import create_app
from app.quality.service import QualityService


def _client():
    return TestClient(create_app(QualityService()))


def test_status_code_mapping(case):
    c = _client()
    case.expect({"非法契约": 422, "冲突契约": 409, "未注册契约": 404})

    r_invalid = c.post("/contracts", json={"name": "x"})
    r_conflict = c.post(
        "/contracts",
        json={"name": "x", "fields": [
            {"name": "n", "type": "integer", "min_value": 9, "max_value": 1}]},
    )
    r_missing = c.post(
        "/contracts/nope/batches/B/validate", json={"rows": [], "batch_id": "B"}
    )
    case.basis(
        (r_invalid.status_code, r_conflict.status_code, r_missing.status_code)
    )
    assert r_invalid.status_code == 422
    assert r_conflict.status_code == 409
    assert r_missing.status_code == 404
    # 错误体必须携带可区分 category 与 reason。
    assert r_invalid.json()["error"]["category"] == "contract_invalid"


def test_validation_endpoint_and_idempotency(case):
    c = _client()
    c.post(
        "/contracts",
        json={"name": "c", "fields": [{"name": "id", "type": "integer"}]},
    )
    payload_bad = {"batch_id": "B1", "rows": [{"id": "not-int"}]}
    case.input("payload", payload_bad)
    case.expect("首次 200 且 passed=False；重复不同体仍返回同一结论")

    r1 = c.post("/contracts/c/batches/B1/validate", json=payload_bad)
    r2 = c.post(
        "/contracts/c/batches/B1/validate",
        json={"batch_id": "B1", "rows": [{"id": 123}]},
    )
    case.basis((r1.json()["violations"], r2.json()["violations"]))
    assert r1.status_code == 200 and r1.json()["passed"] is False
    assert r1.json()["violations"] == r2.json()["violations"]

    trace = c.get("/contracts/c/batches/B1/trace")
    assert trace.status_code == 200
    assert trace.json()["metrics"][0]["metric"] == "completeness"
