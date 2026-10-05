"""基线漂移判定（当前批次 vs 基线批次）。

判定在批次**首次提交**时执行一次并随批次固化：跟哪个基线批次比、
按什么容忍幅度判、每个指标下滑多少、为什么算/不算退步，都写入
:class:`DriftVerdict`，之后不再重算。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.quality.metrics import MetricSnapshot


class DriftStatus(str, Enum):
    """漂移总判。"""

    REGRESSED = "regressed"
    WITHIN_TOLERANCE = "within_tolerance"


class BaselineMode(str, Enum):
    """基线来源：最近一次已判定批次 / 使用方显式指定。"""

    LATEST = "latest"
    EXPLICIT = "explicit"


class DriftMetricResult(BaseModel):
    """单个指标相对基线的对比明细。"""

    metric: str
    dimension: str | None = None
    baseline_batch_id: str
    baseline_value: float
    current_value: float
    decline: float
    tolerance: float
    regressed: bool
    reason: str

    def trace(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "dimension": self.dimension,
            "baseline_batch_id": self.baseline_batch_id,
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "decline": round(self.decline, 6),
            "tolerance": self.tolerance,
            "regressed": self.regressed,
            "reason": self.reason,
        }


class DriftVerdict(BaseModel):
    """一个批次相对基线批次的不可变漂移结论。"""

    contract: str
    batch_id: str
    baseline_batch_id: str
    baseline_mode: BaselineMode
    # 判定时生效的每指标容忍幅度（下滑不超过该值不算退步），固化。
    tolerance: dict[str, float]
    status: DriftStatus
    metrics: list[DriftMetricResult]
    evaluated_at: datetime
    reason: str

    def trace(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "batch_id": self.batch_id,
            "baseline_batch_id": self.baseline_batch_id,
            "baseline_mode": self.baseline_mode.value,
            "status": self.status.value,
            "tolerance": dict(self.tolerance),
            "reason": self.reason,
            "evaluated_at": self.evaluated_at.isoformat(),
            "metrics": [m.trace() for m in self.metrics],
        }


def evaluate_drift(
    contract: str,
    batch_id: str,
    current: list[MetricSnapshot],
    baseline_batch_id: str,
    baseline: list[MetricSnapshot],
    tolerance: dict[str, float],
    mode: BaselineMode,
    evaluated_at: datetime,
) -> DriftVerdict:
    """对比当前批次与基线批次的同名指标，返回固化的漂移结论。

    指标按 ``(metric, dimension)`` 配对（完整性/新鲜度维度为 None，
    唯一性维度为字段名）；仅基线与当前都存在的指标参与对比。
    某指标下滑量 ``baseline_value - current_value`` 超过该指标的
    容忍幅度即判该指标退步；任一指标退步则整批评 ``regressed``。
    """

    # 固化判定时的容忍幅度；缺省指标幅度按 0.0（不允许下滑）处理。
    frozen_tolerance = {name: float(tol) for name, tol in tolerance.items()}

    baseline_index = {(s.metric.value, s.dimension): s for s in baseline}
    results: list[DriftMetricResult] = []
    for snap in current:
        key = (snap.metric.value, snap.dimension)
        base = baseline_index.get(key)
        if base is None:
            # 基线批次没有该指标（例如基线未配置该 unique 字段/新鲜度），
            # 无对比依据，不纳入退步判定。
            continue
        tol = frozen_tolerance.get(snap.metric.value, 0.0)
        decline = round(base.value - snap.value, 6)
        regressed = decline > tol + 1e-9
        if regressed:
            reason = (
                f"{snap.metric.value} 由基线 {base.value} 降至 {snap.value}，"
                f"下滑 {decline} 超过容忍幅度 {tol}"
            )
        else:
            reason = (
                f"{snap.metric.value} 由基线 {base.value} 变为 {snap.value}，"
                f"下滑 {decline} 未超过容忍幅度 {tol}"
            )
        results.append(
            DriftMetricResult(
                metric=snap.metric.value,
                dimension=snap.dimension,
                baseline_batch_id=baseline_batch_id,
                baseline_value=base.value,
                current_value=snap.value,
                decline=decline,
                tolerance=tol,
                regressed=regressed,
                reason=reason,
            )
        )

    regressed_metrics = [
        f"{r.metric}" + (f"({r.dimension})" if r.dimension else "")
        for r in results
        if r.regressed
    ]
    if regressed_metrics:
        status = DriftStatus.REGRESSED
        summary = (
            f"相对基线批次 {baseline_batch_id}，指标 "
            f"{', '.join(regressed_metrics)} 下滑超过容忍幅度"
        )
    else:
        status = DriftStatus.WITHIN_TOLERANCE
        compared = [
            r.metric + (f"({r.dimension})" if r.dimension else "") for r in results
        ]
        summary = (
            f"相对基线批次 {baseline_batch_id}，对比指标 "
            f"[{', '.join(compared)}] 均未超过容忍幅度"
        )

    return DriftVerdict(
        contract=contract,
        batch_id=batch_id,
        baseline_batch_id=baseline_batch_id,
        baseline_mode=mode,
        tolerance=frozen_tolerance,
        status=status,
        metrics=results,
        evaluated_at=evaluated_at,
        reason=summary,
    )
