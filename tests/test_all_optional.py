"""全可选字段契约：合法注册、正常出结论、完整性有明确取值依据。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.quality import metrics as M
from app.quality.service import QualityService

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _all_optional_contract():
    # 只做去重与时效观察的表：所有字段均非必填。
    return {
        "name": "obs",
        "fields": [
            {"name": "id", "type": "integer", "required": False,
             "unique": True},
            {"name": "ts", "type": "timestamp", "required": False},
        ],
        "freshness_field": "ts",
        "freshness_max_age_seconds": 3600,
    }


def test_completeness_metric_without_required_fields(case):
    case.input("required_fields", [])
    case.expect("完整性按定义记 1.0，reason 说明取值依据，不报错")
    snap = M.completeness("B1", "obs", [{"id": 1}], [], 1.0, NOW)
    case.basis((snap.value, snap.healthy, snap.reason))
    assert snap.value == 1.0
    assert snap.healthy is True
    assert "无必填字段" in snap.reason


def test_all_optional_contract_submit_and_trace(case):
    svc = QualityService()
    svc.register_contract(_all_optional_contract())
    rows = [
        {"id": 1, "ts": "2026-10-06T11:59:00Z"},
        {"id": 2},  # 可选字段 ts 整列缺失也允许
        {},          # 全缺也行：没有必填字段
    ]
    case.input("rows", rows)
    case.expect("提交正常返回结论；完整性 1.0 且 reason 可解释；"
                "唯一性/新鲜度照常计算")

    result = svc.submit("obs", "B1", rows, now=NOW)
    snaps = svc._store.get_metrics("obs", "B1")
    comp = [s for s in snaps if s.metric == M.MetricType.COMPLETENESS][0]
    trace = svc.trace("obs", "B1")
    case.basis((result.passed, result.violation_count,
                comp.value, comp.reason,
                [m["metric"] for m in trace["metrics"]]))
    assert result.passed is True
    assert result.violation_count == 0
    assert comp.value == 1.0
    assert "无必填字段" in comp.reason
    # 回溯里能看到完整性取值依据，且唯一性/新鲜度照常留存。
    assert {m["metric"] for m in trace["metrics"]} == {
        "completeness", "uniqueness", "freshness"}
    assert trace["drift"]["baseline_source"] == "none"


def test_all_optional_contract_still_catches_duplicates(case):
    svc = QualityService()
    svc.register_contract(_all_optional_contract())
    rows = [
        {"id": 1, "ts": "2026-10-06T11:59:00Z"},
        {"id": 1, "ts": "2026-10-06T11:58:00Z"},
    ]
    case.input("rows", rows)
    case.expect("全可选不等于不校验：id 重复仍判 unique 违规")
    result = svc.submit("obs", "B1", rows, now=NOW)
    case.basis((result.passed,
                [(v.field, v.rule) for v in result.violations]))
    assert result.passed is False
    assert [(v.field, v.rule) for v in result.violations] == [("id", "unique")]
