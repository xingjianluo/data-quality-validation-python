"""质量指标的历史留存与判定（完整性 / 唯一性 / 刷新时效）。

每个 :class:`MetricSnapshot` 在生成时就固化 ``value / threshold / healthy /
evaluated_at / reason``。漂移与新鲜度阈值的后续变更只影响之后的新批次，
历史快照不可变，从而保证"同一批次无论何时回溯结论一致"。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.quality.contracts import DataType, _parse_temporal
from app.quality.errors import DependencyFailureError


class MetricType(str, Enum):
    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    FRESHNESS = "freshness"


class MetricSnapshot(BaseModel):
    """某批次某项指标的不可变历史快照。"""

    batch_id: str
    contract: str
    metric: MetricType
    value: float
    # 生成快照时生效的判定阈值，后续阈值变更不回写。
    threshold: float
    healthy: bool
    evaluated_at: datetime
    reason: str
    # 新鲜度逐行 SLA（秒）；非新鲜度指标为 None。同样固化。
    max_age_seconds: float | None = None

    def trace(self) -> dict[str, Any]:
        """回溯三元组：契约 + 批次 + 触发原因。"""
        return {
            "contract": self.contract,
            "batch_id": self.batch_id,
            "metric": self.metric.value,
            "value": self.value,
            "threshold": self.threshold,
            "healthy": self.healthy,
            "max_age_seconds": self.max_age_seconds,
            "evaluated_at": self.evaluated_at.isoformat(),
            "reason": self.reason,
        }


def _snapshot(
    metric: MetricType,
    batch_id: str,
    contract: str,
    value: float,
    threshold: float,
    evaluated_at: datetime,
    reason: str,
    max_age_seconds: float | None = None,
) -> MetricSnapshot:
    healthy = value >= threshold
    return MetricSnapshot(
        batch_id=batch_id,
        contract=contract,
        metric=metric,
        value=round(value, 6),
        threshold=threshold,
        healthy=healthy,
        evaluated_at=evaluated_at,
        reason=reason if healthy else f"{reason}; 低于阈值 {threshold}",
        max_age_seconds=max_age_seconds,
    )


def completeness(
    batch_id: str,
    contract: str,
    rows: list[dict],
    required_fields: list[str],
    threshold: float,
    evaluated_at: datetime,
) -> MetricSnapshot:
    """完整率 = 非缺失必填单元数 / (行数 * 必填字段数)。空批次记 0。"""

    if not required_fields:
        raise DependencyFailureError(
            "完整性指标缺少必填字段定义",
            contract=contract,
            batch=batch_id,
            reason="completeness_no_fields",
        )
    total = len(rows) * len(required_fields)
    present = 0
    for row in rows:
        for f in required_fields:
            if f in row and row[f] is not None:
                present += 1
    value = (present / total) if total else 0.0
    return _snapshot(
        MetricType.COMPLETENESS,
        batch_id,
        contract,
        value,
        threshold,
        evaluated_at,
        f"{present}/{total} 必填单元非空",
    )


def uniqueness(
    batch_id: str,
    contract: str,
    rows: list[dict],
    key_field: str,
    threshold: float,
    evaluated_at: datetime,
) -> MetricSnapshot:
    """唯一率 = 去重后非空键数 / 非空键总数；无键时记 1.0。"""

    values: list[Any] = []
    for row in rows:
        if key_field in row and row[key_field] is not None:
            values.append(row[key_field])
    total = len(values)
    distinct = len(set(values))
    value = (distinct / total) if total else 1.0
    return _snapshot(
        MetricType.UNIQUENESS,
        batch_id,
        contract,
        value,
        threshold,
        evaluated_at,
        f"{distinct}/{total} 键唯一",
    )


def freshness(
    batch_id: str,
    contract: str,
    rows: list[dict],
    ts_field: str,
    max_age_seconds: float,
    now: datetime,
    evaluated_at: datetime,
) -> MetricSnapshot:
    """新鲜率 = 未超过 SLA 年龄的行占比。

    健康判据阈值固定为 1.0（所有行都须新鲜）；逐行 SLA
    ``max_age_seconds`` 在生成快照时固化，后续修改不影响历史结论。
    """

    if max_age_seconds < 0:
        raise DependencyFailureError(
            "新鲜度 SLA 不能为负",
            contract=contract,
            batch=batch_id,
            reason="freshness_negative_sla",
        )
    total = len(rows)
    fresh = 0
    for i, row in enumerate(rows):
        if ts_field not in row or row[ts_field] is None:
            # 缺失时间戳计为不新鲜，但不当作依赖故障。
            continue
        ts = _to_datetime(row[ts_field])
        if ts is None:
            raise DependencyFailureError(
                f"第 {i} 行的 {ts_field} 无法解析为时间",
                contract=contract,
                field=ts_field,
                batch=batch_id,
                reason="freshness_bad_timestamp",
            )
        age = (now - ts).total_seconds()
        if age <= max_age_seconds:
            fresh += 1
    value = (fresh / total) if total else 0.0
    return _snapshot(
        MetricType.FRESHNESS,
        batch_id,
        contract,
        value,
        1.0,
        evaluated_at,
        f"{fresh}/{total} 行在 SLA {max_age_seconds}s 内",
        max_age_seconds=max_age_seconds,
    )


def _to_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    if isinstance(raw, str):
        parsed = _parse_temporal(DataType.TIMESTAMP, raw)
        if isinstance(parsed, datetime):
            return parsed
        if isinstance(parsed, date):
            return datetime(parsed.year, parsed.month, parsed.day)
    return None
