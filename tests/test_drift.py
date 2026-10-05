"""基线漂移判定：基线选择、容忍幅度、退步可解释，以及结论固化。

覆盖：
* 默认以最近已判定批次为基线、可显式指定更早批次；
* 容忍幅度按指标配置，超幅度才判退步；
* 漂移结论只在首次判定时固化，新批次 / 改容忍幅度 / 重注册新版本
  都不改写老批次；
* 基线批次不存在 / 未判定 / 指向自身 -> 明确拒绝且不落库；
* 并发与重复提交不重复判定、不重复累计；
* 全可选字段契约正常出结论，完整性有明确取值依据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

import pytest

from app.quality import metrics as M
from app.quality.errors import ErrorCategory, MetricNotFoundError
from app.quality.service import QualityService

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _register(svc: QualityService, *, tolerance=None, sla=None, version=1) -> None:
    contract: dict = {
        "name": "c",
        "version": version,
        "fields": [
            {"name": "id", "type": "integer", "unique": True},
            {"name": "ts", "type": "timestamp"},
        ],
    }
    if sla is not None:
        contract["freshness_field"] = "ts"
        contract["freshness_max_age_seconds"] = sla
    if tolerance is not None:
        contract["drift_tolerance"] = tolerance
    svc.register_contract(contract)


def _drift_metric(trace: dict, metric: str, dimension=None):
    for m in trace["drift"]["metrics"]:
        if m["metric"] == metric and m["dimension"] == dimension:
            return m
    raise AssertionError(f"metric {metric}/{dimension} not in drift")


def test_drift_unit_regressed_beyond_tolerance(case):
    """直接验证 drift 判定单元：下滑超容忍 -> regressed。"""
    from app.quality.drift import BaselineMode, evaluate_drift

    base = [
        M.completeness("B1", "c", [{"id": 1}], ["id"], 1.0, NOW),
    ]
    # 当前完整率 0.5（两行中一行缺 id）。
    cur = [
        M.completeness("B2", "c", [{"id": 1}, {}], ["id"], 1.0, NOW),
    ]
    case.input("values", ("baseline", base[0].value, "current", cur[0].value))
    case.expect("容忍 0.5（边界，不超过）时 within_tolerance；容忍 0.1 时 regressed")
    v_ok = evaluate_drift(
        "c", "B2", cur, "B1", base, {"completeness": 0.5},
        BaselineMode.LATEST, NOW,
    )
    v_bad = evaluate_drift(
        "c", "B2", cur, "B1", base, {"completeness": 0.1},
        BaselineMode.LATEST, NOW,
    )
    case.basis((v_ok.status.value, v_bad.status.value, v_bad.metrics[0].reason))
    assert v_ok.status.value == "within_tolerance"
    assert v_bad.status.value == "regressed"
    assert v_bad.baseline_batch_id == "B1"
    assert "0.5" in v_bad.metrics[0].reason  # 原因里写得清下滑多少、幅度多少


def test_first_batch_has_no_baseline(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1, "ts": "2026-10-06T11:59:00Z"}], now=NOW)
    case.input("batch", "B1")
    case.expect("首个批次无基线，drift=None，指标照常留存")
    trace = svc.trace("c", "B1")
    case.basis(trace["drift"])
    assert trace["drift"] is None
    assert len(trace["metrics"]) == 2  # 完整性 + 唯一性


def test_latest_baseline_detects_regression(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    # B2 出现重复 id：唯一性 0.5，相对 B1(1.0) 明显退步。
    svc.submit("c", "B2", [{"id": 1}, {"id": 1}], now=NOW)
    case.input("B2 rows", [{"id": 1}, {"id": 1}])
    case.expect("基线=最近批次 B1(latest)，唯一性退步 -> regressed")
    trace = svc.trace("c", "B2")
    drift = trace["drift"]
    uniq = _drift_metric(trace, "uniqueness", dimension="id")
    case.basis(
        (drift["baseline_batch_id"], drift["baseline_mode"],
         drift["status"], uniq["decline"], uniq["tolerance"])
    )
    assert drift["baseline_batch_id"] == "B1"
    assert drift["baseline_mode"] == "latest"
    assert drift["status"] == "regressed"
    assert uniq["decline"] == pytest.approx(0.5, abs=1e-6)
    assert uniq["baseline_value"] == 1.0 and uniq["current_value"] == 0.5
    assert uniq["regressed"] is True and uniq["reason"]


def test_within_tolerance_not_regressed(case):
    svc = QualityService()
    # 允许唯一性下滑 0.5 以内。
    _register(svc, tolerance={"uniqueness": 0.5})
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    svc.submit("c", "B2", [{"id": 1}, {"id": 1}], now=NOW)  # 下滑恰好 0.5
    case.input("tolerance", {"uniqueness": 0.5})
    case.expect("下滑 0.5 未超过容忍 0.5 -> within_tolerance")
    trace = svc.trace("c", "B2")
    uniq = _drift_metric(trace, "uniqueness", dimension="id")
    case.basis((trace["drift"]["status"], uniq["decline"], uniq["tolerance"]))
    assert trace["drift"]["status"] == "within_tolerance"
    assert uniq["regressed"] is False


def test_explicit_earlier_baseline(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    svc.submit("c", "B2", [{"id": 3}, {"id": 4}], now=NOW)
    # B3 显式与更早的 B1 对比，而不是默认的 B2。
    svc.submit(
        "c", "B3", [{"id": 5}, {"id": 5}], now=NOW, baseline_batch_id="B1"
    )
    case.input("explicit baseline", "B1")
    case.expect("B3 基线为显式 B1(explicit)，不是最近的 B2")
    trace = svc.trace("c", "B3")
    case.basis((trace["drift"]["baseline_batch_id"], trace["drift"]["baseline_mode"]))
    assert trace["drift"]["baseline_batch_id"] == "B1"
    assert trace["drift"]["baseline_mode"] == "explicit"
    assert trace["drift"]["status"] == "regressed"


def test_unknown_or_unjudged_baseline_rejected(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}], now=NOW)

    case.input("baseline", "NOPE")
    case.expect("metric_not_found / baseline_batch_not_judged，且不落库")
    with pytest.raises(MetricNotFoundError) as info:
        svc.submit("c", "B2", [{"id": 2}], now=NOW, baseline_batch_id="NOPE")
    err = info.value
    case.basis((err.category.value, err.reason, err.batch))
    assert err.category == ErrorCategory.METRIC_NOT_FOUND
    assert err.reason == "baseline_batch_not_judged"
    assert err.batch == "NOPE"
    # 拒绝不能硬凑结论，也不能把 B2 记成已判定批次。
    assert svc._store.has_batch("c", "B2") is False
    assert svc._store.list_batches("c") == ["B1"]

    # 基线指向当前批次自身也拒绝。
    with pytest.raises(MetricNotFoundError) as info2:
        svc.submit("c", "B9", [{"id": 9}], now=NOW, baseline_batch_id="B9")
    case.basis(info2.value.reason)
    assert info2.value.reason == "baseline_is_current_batch"


def test_drift_verdict_frozen_after_first_judgement(case):
    """结论固化：新批次 / 收紧容忍幅度 / 重注册新版本都不改写老批次。"""
    svc = QualityService()
    # 容忍 0.6：B2 唯一性下滑 0.5 不算退步。
    _register(svc, tolerance={"uniqueness": 0.6})
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    svc.submit("c", "B2", [{"id": 1}, {"id": 1}], now=NOW)
    before = svc.trace("c", "B2")["drift"]
    case.input("B2 首次结论", (before["status"], before["tolerance"],
                               before["baseline_batch_id"]))

    # 再来新批次 B3，且重注册同名契约新版本、把容忍收紧到 0.0。
    svc.submit("c", "B3", [{"id": 3}, {"id": 3}], now=NOW)
    _register(svc, tolerance={"uniqueness": 0.0}, version=2)
    # B4 三行全重复：唯一性 1/3，相对 B3(0.5) 再下滑 1/6。
    svc.submit(
        "c", "B4", [{"id": 4}, {"id": 4}, {"id": 4}], now=NOW
    )

    case.expect("B2 结论/基线/幅度与首次判定时完全一致；B3、B4 按新情况判定")
    after_b2 = svc.trace("c", "B2")["drift"]
    after_b3 = svc.trace("c", "B3")["drift"]
    after_b4 = svc.trace("c", "B4")["drift"]
    case.basis(
        ("B2", after_b2["status"], after_b2["tolerance"], after_b2["baseline_batch_id"]),
    )
    assert after_b2 == before
    assert after_b2["status"] == "within_tolerance"
    assert after_b2["tolerance"] == {"uniqueness": 0.6}
    assert after_b2["baseline_batch_id"] == "B1"
    # B3 在收紧前注册，用首次判定时的 0.6；B4 用新版本的 0.0 -> regressed。
    assert after_b3["tolerance"] == {"uniqueness": 0.6}
    assert after_b4["tolerance"] == {"uniqueness": 0.0}
    assert after_b4["status"] == "regressed"


def test_repeated_submit_does_not_rejudge(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    svc.submit("c", "B2", [{"id": 1}, {"id": 1}], now=NOW)
    verdict_first = svc._store.get_verdict("c", "B2")
    case.expect("重复提交（即使带不同基线参数/数据）仍返回首次固化结论")

    # 重复提交 B2：换显式基线、换数据，都必须被忽略。
    svc.submit(
        "c", "B2", [{"id": 999}], now=NOW, baseline_batch_id="B1"
    )
    verdict_again = svc._store.get_verdict("c", "B2")
    trace = svc.trace("c", "B2")
    case.basis(
        (verdict_again is verdict_first,
         trace["drift"]["baseline_mode"],
         len(svc._store.get_metrics("c", "B2")))
    )
    assert verdict_again is verdict_first
    assert trace["drift"]["baseline_mode"] == "latest"  # 没被 explicit 改写
    assert len(svc._store.get_metrics("c", "B2")) == 2  # 指标不重复累计


def test_concurrent_submit_single_verdict(case):
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}, {"id": 2}], now=NOW)
    rows = [{"id": 1}, {"id": 1}]
    case.input("threads", 16)
    case.expect("16 线程并发提交 B2：只判定一次、指标只累计一次、结论同一")

    verdicts: list = []
    results: list = []
    barrier = Barrier(16)

    def worker():
        barrier.wait()
        results.append(svc.submit("c", "B2", rows, now=NOW))
        verdicts.append(svc._store.get_verdict("c", "B2"))

    threads = [Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    case.basis(
        (len(svc._store.get_metrics("c", "B2")),
         all(v is verdicts[0] for v in verdicts),
         svc._store.list_batches("c"))
    )
    assert len(svc._store.get_metrics("c", "B2")) == 2
    assert all(v is verdicts[0] for v in verdicts)
    assert svc._store.list_batches("c") == ["B1", "B2"]
    assert all(r is results[0] for r in results)


def test_concurrent_distinct_batches_have_determined_baselines(case):
    """不同批次并发：每个批次都能找到确定基线，且各自只判定一次。"""
    svc = QualityService()
    _register(svc)
    svc.submit("c", "B1", [{"id": 1}], now=NOW)
    barrier = Barrier(2)

    def submit(batch):
        barrier.wait()
        svc.submit("c", batch, [{"id": 1}, {"id": 2}], now=NOW)

    threads = [Thread(target=submit, args=(b,)) for b in ("B2", "B3")]
    [t.start() for t in threads]
    [t.join() for t in threads]

    case.expect("B2、B3 均有漂移结论且不重样；其中一个以另一个为最近基线")
    v2 = svc._store.get_verdict("c", "B2")
    v3 = svc._store.get_verdict("c", "B3")
    baselines = sorted((v2.baseline_batch_id, v3.baseline_batch_id))
    case.basis(baselines)
    assert v2 is not None and v3 is not None
    assert v2.batch_id != v3.batch_id
    # 一个基线是 B1，另一个基线是 B2/B3 中的先判者。
    assert "B1" in (v2.baseline_batch_id, v3.baseline_batch_id)


def test_all_optional_contract_completeness(case):
    """全可选字段契约（只做去重/时效观察）合法，提交正常出结论。"""
    svc = QualityService()
    svc.register_contract(
        {
            "name": "obs",
            "fields": [
                {"name": "tag", "type": "string", "required": False,
                 "nullable": True, "unique": True},
                {"name": "ts", "type": "timestamp", "required": False,
                 "nullable": True},
            ],
            "freshness_field": "ts",
            "freshness_max_age_seconds": 3600,
        }
    )
    rows = [
        {"tag": "a", "ts": "2026-10-06T11:59:00Z"},
        {"ts": None},
        {},
    ]
    case.input("rows(all optional)", rows)
    case.expect("提交成功；完整性=1.0 且 reason 说明取值依据；不报错")
    result = svc.submit("obs", "B1", rows, now=NOW)
    trace = svc.trace("obs", "B1")
    comp = next(m for m in trace["metrics"] if m["metric"] == "completeness")
    case.basis((result.passed, comp["value"], comp["healthy"], comp["reason"]))
    assert result.total_rows == 3
    assert comp["value"] == 1.0 and comp["healthy"] is True
    assert "无必填字段" in comp["reason"]
    # 唯一性、新鲜度指标也照常产生。
    assert {m["metric"] for m in trace["metrics"]} == {
        "completeness", "uniqueness", "freshness"
    }


def test_all_optional_empty_batch_does_not_raise(case):
    svc = QualityService()
    svc.register_contract(
        {
            "name": "obs",
            "fields": [{"name": "tag", "type": "string", "required": False}],
        }
    )
    case.input("rows", [])
    case.expect("空批次也能出结论，完整性 1.0（无必填单元）")
    svc.submit("obs", "B1", [], now=NOW)
    comp = next(
        m for m in svc.trace("obs", "B1")["metrics"]
        if m["metric"] == "completeness"
    )
    case.basis((comp["value"], comp["reason"]))
    assert comp["value"] == 1.0
