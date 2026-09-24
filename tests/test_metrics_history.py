"""质量指标：历史留存、漂移/新鲜度判定、阈值变更不篡改历史、可回溯。"""

from __future__ import annotations

from conftest import log_case
from dqv.contracts import parse_contract
from dqv.metrics import MetricName, MetricsStore, evaluate_quality

FRESH_CONTRACT = {"name": "orders", "fields": [
    {"name": "id", "type": "integer", "constraints": {"unique": True}},
    {"name": "ts", "type": "timestamp"},
], "freshness_max_age_seconds": 100}


def _snap(store, batch, metric):
    hits = [s for s in store.all_snapshots()
            if s.batch_id == batch and s.metric is metric]
    assert len(hits) == 1
    return hits[0]


class TestCompletenessAndUniqueness:
    def test_metrics_and_basis(self):
        c = parse_contract(FRESH_CONTRACT)
        store = MetricsStore()
        records = [{"id": 1, "ts": 1000}, {"id": None, "ts": 1000},
                   {"id": 1, "ts": 1000}]
        snaps = evaluate_quality(c, records, "b1", now_epoch=1050, store=store)
        values = {s.metric.value: (s.value, s.passed) for s in snaps}
        log_case("metrics", records,
                 "completeness=5/6 fail@1.0, uniqueness=0.5, freshness=50 pass, drift=0 pass",
                 {k: v for k, v in values.items()})
        assert values["completeness"] == (round(5 / 6, 6), False)
        assert values["uniqueness"] == (0.5, False)
        assert values["freshness"] == (50.0, True)
        assert values["completeness_drift"] == (0.0, True)


class TestDrift:
    def test_drift_uses_previous_baseline(self):
        c = parse_contract(FRESH_CONTRACT)
        store = MetricsStore()
        evaluate_quality(c, [{"id": 1, "ts": 1000}] * 4, "b1",
                         now_epoch=1050, store=store, drift_max_drop=0.1)
        evaluate_quality(c, [{"id": 1, "ts": 1000}] * 3 +
                         [{}],
                         "b2", now_epoch=1050, store=store, drift_max_drop=0.1)
        drift = _snap(store, "b2", MetricName.COMPLETENESS_DRIFT)
        log_case("drift", "b1 完整率 1.0 -> b2 完整率 0.75",
                 "drop=0.25 > 0.1 -> fail", drift.reason)
        assert drift.value == 0.25 and drift.passed is False
        assert "b1" in drift.reason


class TestFreshness:
    def test_stale_data_fails_and_missing_timestamp_fails(self):
        c = parse_contract(FRESH_CONTRACT)
        store = MetricsStore()
        evaluate_quality(c, [{"id": 1, "ts": 1000}], "stale",
                         now_epoch=2000, store=store)
        stale = _snap(store, "stale", MetricName.FRESHNESS)
        log_case("stale", "refresh_ts=1000, now=2000, threshold=100",
                 "age=1000 > 100 -> fail", stale.reason)
        assert stale.passed is False and stale.value == 1000.0

        c2 = parse_contract({"name": "no_ts", "freshness_max_age_seconds": 10,
                             "fields": [{"name": "id", "type": "integer"}]})
        evaluate_quality(c2, [{"id": 1}], "no-ts", now_epoch=1.0, store=store)
        missing = _snap(store, "no-ts", MetricName.FRESHNESS)
        log_case("no-timestamp", "声明新鲜度阈值但无 timestamp 字段",
                 "freshness fail（inf），不得静默通过", missing.reason)
        assert missing.passed is False


class TestHistoryImmutability:
    def test_threshold_change_does_not_rewrite_history(self):
        c = parse_contract(FRESH_CONTRACT)
        store = MetricsStore()
        evaluate_quality(c, [{"id": None, "ts": 1000}], "b1",
                         now_epoch=1050, store=store,
                         completeness_min=1.0, drift_max_drop=0.0)
        evaluate_quality(c, [{"id": None, "ts": 1000}], "b2",
                         now_epoch=1050, store=store,
                         completeness_min=0.1, drift_max_drop=0.9)
        b1_comp = _snap(store, "b1", MetricName.COMPLETENESS)
        b1_drift = _snap(store, "b1", MetricName.COMPLETENESS_DRIFT)
        b2_comp = _snap(store, "b2", MetricName.COMPLETENESS)
        log_case("threshold-change",
                 "b1 阈值 1.0/0.0；之后改为 0.1/0.9 再跑 b2",
                 "b1 历史快照的 threshold/passed 不变",
                 {"b1_comp": (b1_comp.threshold, b1_comp.passed),
                  "b1_drift": (b1_drift.threshold, b1_drift.passed),
                  "b2_comp": (b2_comp.threshold, b2_comp.passed)})
        assert b1_comp.threshold == 1.0 and b1_comp.passed is False
        assert b1_drift.threshold == 0.0
        assert b2_comp.threshold == 0.1 and b2_comp.passed is True
        # 历史列表只追加
        assert [s.batch_id for s in store.history("orders")][:4] == ["b1"] * 4

    def test_snapshots_trace_back_to_contract_and_batch(self):
        c = parse_contract(FRESH_CONTRACT)
        store = MetricsStore()
        snaps = evaluate_quality(c, [{"id": 1, "ts": 1000}], "batch-xyz",
                                 now_epoch=1050, store=store)
        for s in snaps:
            log_case("trace", f"snapshot {s.metric.value}",
                     "含 contract_name/version/batch_id/reason",
                     s.to_dict())
            assert s.contract_name == "orders"
            assert s.contract_version == 1
            assert s.batch_id == "batch-xyz"
            assert s.reason
