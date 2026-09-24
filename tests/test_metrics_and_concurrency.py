"""指标历史、阈值不可篡改，以及并发/重复提交的幂等性。"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

import pytest

from app.quality import metrics as M
from app.quality.errors import ErrorCategory, MetricNotFoundError
from app.quality.service import QualityService

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def test_completeness_value(case):
    rows = [{"id": 1, "name": "x"}, {"id": None, "name": "y"}]
    case.input("rows", rows)
    case.expect("完整率 0.75，阈值 1.0 -> 不健康")
    snap = M.completeness("B1", "c", rows, ["id", "name"], 1.0, NOW)
    case.basis((snap.value, snap.healthy, snap.reason))
    assert snap.value == 0.75
    assert snap.healthy is False


def test_uniqueness_value(case):
    rows = [{"id": 1}, {"id": 1}, {"id": 2}]
    case.input("rows", rows)
    case.expect("唯一率 2/3")
    snap = M.uniqueness("B1", "c", rows, "id", 1.0, NOW)
    case.basis((snap.value, snap.reason))
    assert snap.value == pytest.approx(2 / 3, abs=1e-6)
    assert snap.healthy is False


def test_freshness_value_and_frozen_sla(case):
    rows = [
        {"ts": "2026-09-24T11:59:00Z"},
        {"ts": "2026-09-24T10:00:00Z"},
    ]
    case.input("rows", rows)
    case.expect("SLA 3600s 下新鲜率 0.5，SLA 固化为 3600")
    snap = M.freshness("B1", "c", rows, "ts", 3600, NOW, NOW)
    case.basis((snap.value, snap.max_age_seconds, snap.reason))
    assert snap.value == 0.5
    assert snap.max_age_seconds == 3600


def test_bad_timestamp_is_dependency_failure(case):
    rows = [{"ts": "not-a-time"}]
    case.input("rows", rows)
    case.expect(ErrorCategory.DEPENDENCY_FAILURE)
    with pytest.raises(Exception) as info:
        M.freshness("B1", "c", rows, "ts", 3600, NOW, NOW)
    case.basis(info.value.category.value)
    assert info.value.category == ErrorCategory.DEPENDENCY_FAILURE


def _service_with_contract(sla=3600):
    svc = QualityService()
    svc.register_contract(
        {
            "name": "c",
            "fields": [
                {"name": "id", "type": "integer", "unique": True},
                {"name": "ts", "type": "timestamp"},
            ],
            "freshness_field": "ts",
            "freshness_max_age_seconds": sla,
        }
    )
    return svc


def test_concurrent_submit_single_result_and_metrics(case):
    svc = _service_with_contract()
    rows = [
        {"id": 1, "ts": "2026-09-24T11:59:00Z"},
        {"id": 2, "ts": "2026-09-24T11:30:00Z"},
    ]
    case.input("threads", 16)
    case.expect("16 个并发只产生 1 个批次、3 个指标快照，且结论同一")

    results: list = []
    barrier = Barrier(16)

    def worker():
        barrier.wait()
        results.append(svc.submit("c", "B1", rows, now=NOW))

    threads = [Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    batches = svc._store.list_batches("c")
    metric_count = len(svc._store.get_metrics("c", "B1"))
    case.basis((batches, metric_count, all(r is results[0] for r in results)))
    assert batches == ["B1"]
    assert metric_count == 3  # 完整性 + 唯一性 + 新鲜度，只累计一次
    assert all(r is results[0] for r in results)


def test_duplicate_submit_consistent_and_not_double_counted(case):
    svc = _service_with_contract()
    rows_ok = [{"id": 1, "ts": "2026-09-24T11:59:00Z"}]
    # 第二次故意提交不同数据，也必须返回首次结论（幂等键以批次为准）。
    rows_different = [{"id": 999, "ts": "2020-01-01T00:00:00Z"}]
    case.input("first", rows_ok)
    case.input("duplicate", rows_different)
    case.expect("重复提交返回首次结论，指标数保持 3")

    first = svc.submit("c", "B1", rows_ok, now=NOW)
    second = svc.submit("c", "B1", rows_different, now=NOW)
    case.basis(
        (first.model_dump() == second.model_dump(),
         len(svc._store.get_metrics("c", "B1")))
    )
    assert first == second
    assert len(svc._store.get_metrics("c", "B1")) == 3


def test_threshold_change_does_not_rewrite_history(case):
    svc = _service_with_contract(sla=3600)
    rows = [{"id": 1, "ts": "2026-09-24T11:30:00Z"}]  # age 1800s
    svc.submit("c", "B1", rows, now=NOW)
    before = svc._store.get_metrics("c", "B1")
    f_before = [m for m in before if m.metric == M.MetricType.FRESHNESS][0]
    case.input("B1 sla", 3600)
    case.expect("收紧 SLA 到 600 后，B1 仍健康且 SLA 保持 3600；B2 才不健康")

    # 收紧阈值，重新注册同名契约（新版本）。
    svc.register_contract(
        {
            "name": "c",
            "version": 2,
            "fields": [
                {"name": "id", "type": "integer", "unique": True},
                {"name": "ts", "type": "timestamp"},
            ],
            "freshness_field": "ts",
            "freshness_max_age_seconds": 600,
        }
    )
    svc.submit("c", "B2", rows, now=NOW)

    f_b1 = [
        m for m in svc._store.get_metrics("c", "B1") if m.metric == M.MetricType.FRESHNESS
    ][0]
    f_b2 = [
        m for m in svc._store.get_metrics("c", "B2") if m.metric == M.MetricType.FRESHNESS
    ][0]
    case.basis(
        (("B1", f_b1.healthy, f_b1.max_age_seconds),
         ("B2", f_b2.healthy, f_b2.max_age_seconds))
    )
    assert f_b1.healthy is True and f_b1.max_age_seconds == 3600
    assert f_b2.healthy is False and f_b2.max_age_seconds == 600


def test_trace_links_contract_batch_reason(case):
    svc = _service_with_contract()
    rows = [{"id": 1, "ts": "2026-09-24T11:59:00Z"}]
    svc.submit("c", "B1", rows, now=NOW)
    case.input("batch", "B1")
    case.expect("trace 含契约、批次、各指标 reason")
    trace = svc.trace("c", "B1")
    reasons = [m["reason"] for m in trace["metrics"]]
    case.basis((trace["contract"], trace["batch_id"], reasons))
    assert trace["contract"] == "c" and trace["batch_id"] == "B1"
    assert all(reasons)


def test_trace_unknown_batch_not_found(case):
    svc = _service_with_contract()
    case.input("batch", "NOPE")
    case.expect(ErrorCategory.METRIC_NOT_FOUND)
    with pytest.raises(MetricNotFoundError) as info:
        svc.trace("c", "NOPE")
    case.basis(info.value.category.value)
    assert info.value.category == ErrorCategory.METRIC_NOT_FOUND
