"""基线漂移判定：对比、固化、拒绝与并发幂等。"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

import pytest

from app.quality.drift import DriftStatus
from app.quality.errors import ErrorCategory, MetricNotFoundError
from app.quality.service import QualityService

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _contract(tolerance=0.0, version=1):
    return {
        "name": "c",
        "version": version,
        "fields": [
            {"name": "id", "type": "integer", "unique": True},
            {"name": "name", "type": "string"},
        ],
        "drift_tolerance": tolerance,
    }


def _service(tolerance=0.0):
    svc = QualityService()
    svc.register_contract(_contract(tolerance))
    return svc


def _rows(*names):
    """names 为 None 表示该行 name 缺失（拉低完整性）。"""
    return [
        {"id": i + 1, **({"name": n} if n is not None else {})}
        for i, n in enumerate(names)
    ]


def test_first_batch_has_no_baseline(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    drift = svc._store.get_drift("c", "B1")
    case.input("batch", "B1（首个批次）")
    case.expect("baseline_source=none，所有指标 no_baseline，不判退步")
    case.basis((drift.baseline_source, drift.baseline_batch_id,
                [c.status.value for c in drift.comparisons]))
    assert drift.baseline_source == "none"
    assert drift.baseline_batch_id is None
    assert all(c.status is DriftStatus.NO_BASELINE for c in drift.comparisons)
    assert drift.regressed is False


def test_latest_baseline_no_regression(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    svc.submit("c", "B2", _rows("c", "d"), now=NOW)
    drift = svc._store.get_drift("c", "B2")
    case.input("batches", "B1 完整 -> B2 完整")
    case.expect("B2 自动以 B1 为基线，指标无下滑，全部 ok")
    case.basis((drift.baseline_source, drift.baseline_batch_id,
                [(c.metric.value, c.status.value, c.reason)
                 for c in drift.comparisons]))
    assert drift.baseline_source == "latest"
    assert drift.baseline_batch_id == "B1"
    assert all(c.status is DriftStatus.OK for c in drift.comparisons)
    assert drift.regressed is False


def test_regression_beyond_tolerance_detected(case):
    svc = _service(tolerance=0.0)
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)          # 完整性 1.0
    svc.submit("c", "B2", _rows("c", None), now=NOW)         # 完整性 0.75
    drift = svc._store.get_drift("c", "B2")
    comp = [c for c in drift.comparisons if c.metric.value == "completeness"][0]
    case.input("baseline/completeness", ("B1", 1.0))
    case.input("current/completeness", ("B2", 0.75))
    case.expect("容忍 0，下滑 0.25 -> regressed，结论写明基线与幅度")
    case.basis((comp.status.value, comp.baseline_batch_id,
                comp.baseline_value, comp.current_value,
                comp.tolerance, comp.reason))
    assert comp.status is DriftStatus.REGRESSED
    assert comp.baseline_batch_id == "B1"
    assert comp.baseline_value == 1.0
    assert comp.current_value == 0.75
    assert comp.tolerance == 0.0
    assert "B1" in comp.reason and "0.25" in comp.reason
    assert drift.regressed is True


def test_tolerance_allows_small_drop(case):
    svc = _service(tolerance=0.3)
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    svc.submit("c", "B2", _rows("c", None), now=NOW)  # 下滑 0.25 < 容忍 0.3
    drift = svc._store.get_drift("c", "B2")
    comp = [c for c in drift.comparisons if c.metric.value == "completeness"][0]
    case.input("tolerance", 0.3)
    case.input("drop", 0.25)
    case.expect("下滑在容忍幅度内 -> ok，且幅度被固化进结论")
    case.basis((comp.status.value, comp.tolerance, comp.reason))
    assert comp.status is DriftStatus.OK
    assert comp.tolerance == 0.3
    assert drift.regressed is False


def test_explicit_baseline_older_batch(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    svc.submit("c", "B2", _rows("c", "d"), now=NOW)
    svc.submit("c", "B3", _rows("e", None), now=NOW,
               baseline_batch_id="B1")
    drift = svc._store.get_drift("c", "B3")
    case.input("explicit baseline", "B1（跳过更近的 B2）")
    case.expect("B3 与显式指定的 B1 比，baseline_source=explicit")
    case.basis((drift.baseline_source, drift.baseline_batch_id,
                [(c.metric.value, c.status.value) for c in drift.comparisons]))
    assert drift.baseline_source == "explicit"
    assert drift.baseline_batch_id == "B1"
    comp = [c for c in drift.comparisons if c.metric.value == "completeness"][0]
    assert comp.status is DriftStatus.REGRESSED


def test_explicit_baseline_not_evaluated_rejected(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a"), now=NOW)
    case.input("baseline", "NOPE（不存在/未判定）")
    case.expect("明确拒绝 metric_not_found/baseline_not_evaluated，"
                "且 B2 不被判定")
    with pytest.raises(MetricNotFoundError) as info:
        svc.submit("c", "B2", _rows("b"), now=NOW, baseline_batch_id="NOPE")
    case.basis((info.value.category.value, info.value.reason,
                svc._store.get_result("c", "B2"),
                svc._store.get_drift("c", "B2")))
    assert info.value.category == ErrorCategory.METRIC_NOT_FOUND
    assert info.value.reason == "baseline_not_evaluated"
    # 拒绝基线时不得硬凑结论：B2 没有任何判定痕迹。
    assert svc._store.get_result("c", "B2") is None
    assert svc._store.get_drift("c", "B2") is None


def test_drift_frozen_against_new_batch_and_tolerance_change(case):
    svc = _service(tolerance=0.0)
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    svc.submit("c", "B2", _rows("c", None), now=NOW)  # vs B1，regressed
    before = svc._store.get_drift("c", "B2").model_dump()
    case.input("B2 初判", before["comparisons"][0]["status"]
               if before["comparisons"] else None)
    case.expect("之后新批次 + 调宽容忍 + 重注册契约，B2 结论不变")

    # 调宽容忍幅度并重新注册同名契约（新版本），再跑新批次。
    svc.register_contract(_contract(tolerance=0.9, version=2))
    svc.submit("c", "B3", _rows("e", "f"), now=NOW)

    after = svc._store.get_drift("c", "B2").model_dump()
    case.basis((before == after,
                svc._store.get_drift("c", "B3").tolerance))
    assert after == before  # 老批次结论、基线、幅度全部冻结
    assert svc._store.get_drift("c", "B3").tolerance == 0.9  # 只影响新批次


def test_concurrent_submit_single_drift_report(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    rows = _rows("c", None)
    case.input("threads", 16)
    case.expect("16 个并发提交 B2：只有 1 份漂移报告，结论同一")

    results: list = []
    barrier = Barrier(16)

    def worker():
        barrier.wait()
        results.append(svc.submit("c", "B2", rows, now=NOW))

    threads = [Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    drift = svc._store.get_drift("c", "B2")
    case.basis((svc._store.list_batches("c"),
                len(svc._store.get_metrics("c", "B2")),
                drift.baseline_batch_id,
                all(r is results[0] for r in results)))
    assert svc._store.list_batches("c") == ["B1", "B2"]
    assert len(svc._store.get_metrics("c", "B2")) == 2  # 完整性+唯一性各一次
    assert drift is not None and drift.baseline_batch_id == "B1"
    assert all(r is results[0] for r in results)


def test_duplicate_submit_keeps_first_drift_verdict(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a", "b"), now=NOW)
    svc.submit("c", "B2", _rows("c", None), now=NOW)  # 首次判定，vs B1
    first = svc._store.get_drift("c", "B2").model_dump()
    case.input("duplicate", "同 B2 再提交，且故意指定另一个基线 B1->显式")
    case.expect("重复提交不重判：漂移结论与首次完全一致")

    # 重复提交即使带上不同的基线参数，也不得改写已固化的结论。
    svc.submit("c", "B2", _rows("z"), now=NOW, baseline_batch_id="B1")
    second = svc._store.get_drift("c", "B2").model_dump()
    case.basis((first == second, len(svc._store.get_metrics("c", "B2"))))
    assert second == first
    assert len(svc._store.get_metrics("c", "B2")) == 2  # 未重复累计


def test_trace_appends_drift_without_breaking_old_fields(case):
    svc = _service()
    svc.submit("c", "B1", _rows("a"), now=NOW)
    svc.submit("c", "B2", _rows("b"), now=NOW)
    trace = svc.trace("c", "B2")
    case.input("trace", "c/B2")
    case.expect("老字段 contract/batch_id/validation/metrics 保持，"
                "新增 drift 说明基线与幅度")
    case.basis((sorted(trace.keys()), trace["drift"]["baseline_batch_id"],
                trace["drift"]["baseline_source"]))
    assert trace["contract"] == "c" and trace["batch_id"] == "B2"
    assert set(trace["validation"]) == {
        "passed", "total_rows", "violation_count", "violations"}
    assert trace["drift"]["baseline_batch_id"] == "B1"
    assert trace["drift"]["baseline_source"] == "latest"
    assert trace["drift"]["regressed"] is False
