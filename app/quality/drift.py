"""基线漂移判定：当前批次指标 vs 基线批次指标。

判定结论（:class:`DriftReport`）在批次首次判定时生成一次并固化进历史，
之后新批次产生、容忍幅度调整、同名契约重新注册，都不会回写它。

* 基线来源：``explicit``（调用方显式指定历史批次）或 ``latest``
  （该契约此前最近一次已判定的批次）；首个批次没有基线，记 ``none``；
* 判定幅度：``tolerance`` 为允许下滑的最大幅度（绝对值，如 0.05 表示
  指标值最多允许比基线低 0.05），判定时刻从契约读取并固化；
* 指标按 ``(metric, field)`` 对齐比较；基线缺少同口径指标时该指标记
  ``no_baseline_metric``，不硬凑结论。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.quality.metrics import MetricSnapshot, MetricType


class DriftStatus(str, Enum):
    """单项指标的漂移判定结论。"""

    OK = "ok"
    """未退步（下滑幅度在容忍范围内或有所上升）。"""

    REGRESSED = "regressed"
    """退步：当前值低于基线值且超出容忍幅度。"""

    NO_BASELINE = "no_baseline"
    """无基线批次可比（通常是该契约的首个批次）。"""

    NO_BASELINE_METRIC = "no_baseline_metric"
    """基线批次存在，但缺少同口径指标，无法比较。"""


class DriftComparison(BaseModel):
    """单项指标与基线的对比结论（不可变）。"""

    metric: MetricType
    field: str | None = None
    baseline_batch_id: str | None = None
    baseline_value: float | None = None
    current_value: float
    tolerance: float
    status: DriftStatus
    reason: str

    def trace(self) -> dict[str, Any]:
        return {
            "metric": self.metric.value,
            "field": self.field,
            "baseline_batch_id": self.baseline_batch_id,
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "tolerance": self.tolerance,
            "status": self.status.value,
            "reason": self.reason,
        }


class DriftReport(BaseModel):
    """一个批次的整体漂移判定结论（首次判定时固化）。"""

    batch_id: str
    contract: str
    # 实际用于对比的基线批次；无基线时为 None。
    baseline_batch_id: str | None
    baseline_source: str  # "explicit" | "latest" | "none"
    # 判定时刻生效的容忍幅度，固化后不回写。
    tolerance: float
    evaluated_at: datetime
    comparisons: list[DriftComparison]

    @property
    def regressed(self) -> bool:
        return any(c.status is DriftStatus.REGRESSED for c in self.comparisons)

    def trace(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "batch_id": self.batch_id,
            "baseline_batch_id": self.baseline_batch_id,
            "baseline_source": self.baseline_source,
            "tolerance": self.tolerance,
            "regressed": self.regressed,
            "evaluated_at": self.evaluated_at.isoformat(),
            "comparisons": [c.trace() for c in self.comparisons],
        }


def compare_with_baseline(
    *,
    contract: str,
    batch_id: str,
    current: list[MetricSnapshot],
    baseline_batch_id: str | None,
    baseline: list[MetricSnapshot] | None,
    baseline_source: str,
    tolerance: float,
    evaluated_at: datetime,
) -> DriftReport:
    """把当前批次指标与基线批次指标按 (metric, field) 对齐比较。

    ``baseline`` 为 None 表示没有可比的基线批次，所有指标记
    ``no_baseline``；基线存在但缺同口径指标时记 ``no_baseline_metric``。
    """

    baseline_index: dict[tuple[MetricType, str | None], MetricSnapshot] = {}
    for snap in baseline or []:
        baseline_index[(snap.metric, snap.field)] = snap

    comparisons: list[DriftComparison] = []
    for snap in current:
        key = (snap.metric, snap.field)
        base = baseline_index.get(key)
        if baseline is None:
            comparisons.append(
                DriftComparison(
                    metric=snap.metric,
                    field=snap.field,
                    baseline_batch_id=None,
                    baseline_value=None,
                    current_value=snap.value,
                    tolerance=tolerance,
                    status=DriftStatus.NO_BASELINE,
                    reason="该契约此前没有已判定批次，无基线可比较",
                )
            )
            continue
        if base is None:
            comparisons.append(
                DriftComparison(
                    metric=snap.metric,
                    field=snap.field,
                    baseline_batch_id=baseline_batch_id,
                    baseline_value=None,
                    current_value=snap.value,
                    tolerance=tolerance,
                    status=DriftStatus.NO_BASELINE_METRIC,
                    reason=f"基线批次 {baseline_batch_id} 缺少同口径指标 "
                    f"{snap.metric.value}"
                    + (f"(字段 {snap.field})" if snap.field else ""),
                )
            )
            continue
        drop = round(base.value - snap.value, 6)
        if drop > tolerance:
            status = DriftStatus.REGRESSED
            reason = (
                f"较基线批次 {baseline_batch_id} 下滑 {drop}，"
                f"超出容忍幅度 {tolerance}"
            )
        else:
            status = DriftStatus.OK
            reason = (
                f"较基线批次 {baseline_batch_id} 变化 {-drop:+}，"
                f"在容忍幅度 {tolerance} 内"
            )
        comparisons.append(
            DriftComparison(
                metric=snap.metric,
                field=snap.field,
                baseline_batch_id=baseline_batch_id,
                baseline_value=base.value,
                current_value=snap.value,
                tolerance=tolerance,
                status=status,
                reason=reason,
            )
        )

    return DriftReport(
        batch_id=batch_id,
        contract=contract,
        baseline_batch_id=baseline_batch_id if baseline is not None else None,
        baseline_source=baseline_source,
        tolerance=tolerance,
        evaluated_at=evaluated_at,
        comparisons=comparisons,
    )
