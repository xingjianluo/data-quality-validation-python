"""质量指标（完整性/唯一性/新鲜度）的计算、判定与历史留存。

设计原则：
* 每次判定产生不可变 MetricSnapshot，连同当时使用的阈值一并固化；
  事后修改漂移/新鲜度阈值，不会重算、篡改任何历史结论。
* MetricsStore 仅追加；同一 (batch_id, metric) 重复写入会被拒绝，
  作为并发/重复提交不重复累计的最后一道防线。
* 每个快照都携带 contract_name / contract_version / batch_id 与
  触发判定的 reason，异常可从指标一路回溯到契约与执行批次。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from dqv.contracts import Contract, FieldType


class MetricName(str, Enum):
    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    FRESHNESS = "freshness"
    COMPLETENESS_DRIFT = "completeness_drift"


@dataclass(frozen=True)
class MetricSnapshot:
    """一次指标判定的不可变快照；阈值随快照固化，历史结论不被篡改。"""

    batch_id: str
    contract_name: str
    contract_version: int
    metric: MetricName
    value: float
    threshold: float
    passed: bool
    reason: str
    evaluated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "metric": self.metric.value,
            "value": self.value,
            "threshold": self.threshold,
            "passed": self.passed,
            "reason": self.reason,
            "evaluated_at": self.evaluated_at,
        }


@dataclass
class MetricsStore:
    """仅追加的指标历史库。"""

    _snapshots: list[MetricSnapshot] = field(default_factory=list)

    def append(self, snapshot: MetricSnapshot) -> None:
        key = (snapshot.batch_id, snapshot.metric)
        if any((s.batch_id, s.metric) == key for s in self._snapshots):
            raise ValueError(f"指标 {snapshot.metric.value}@batch={snapshot.batch_id} 已存在，禁止重复累计")
        self._snapshots.append(snapshot)

    def history(self, contract_name: str) -> list[MetricSnapshot]:
        return [s for s in self._snapshots if s.contract_name == contract_name]

    def latest(self, contract_name: str, metric: MetricName) -> MetricSnapshot | None:
        candidates = [
            s for s in self._snapshots
            if s.contract_name == contract_name and s.metric is metric
        ]
        return candidates[-1] if candidates else None

    def all_snapshots(self) -> list[MetricSnapshot]:
        return list(self._snapshots)


def evaluate_quality(
    contract: Contract,
    records: list[dict[str, Any]],
    batch_id: str,
    *,
    now_epoch: float | None = None,
    store: MetricsStore | None = None,
    completeness_min: float = 1.0,
    uniqueness_min: float = 1.0,
    drift_max_drop: float = 0.0,
) -> list[MetricSnapshot]:
    """计算完整性、唯一性、新鲜度（含完整性漂移判定），并追加历史。

    阈值参数只影响本次判定并固化进快照；store 中既有历史绝不重算。
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    store = store or MetricsStore()
    evaluated_at = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()

    def make(metric: MetricName, value: float, threshold: float, passed: bool, reason: str) -> MetricSnapshot:
        return MetricSnapshot(
            batch_id=batch_id,
            contract_name=contract.name,
            contract_version=contract.version,
            metric=metric,
            value=round(value, 6),
            threshold=threshold,
            passed=passed,
            reason=reason,
            evaluated_at=evaluated_at,
        )

    completeness, c_reason = _completeness(contract, records)
    uniqueness, u_reason = _uniqueness(contract, records)

    snapshots: list[MetricSnapshot] = [
        make(
            MetricName.COMPLETENESS,
            completeness,
            completeness_min,
            completeness >= completeness_min,
            c_reason,
        ),
        make(
            MetricName.UNIQUENESS,
            uniqueness,
            uniqueness_min,
            uniqueness >= uniqueness_min,
            u_reason,
        ),
    ]

    if contract.freshness_max_age_seconds is not None:
        age, f_reason = _freshness(contract, records, now_epoch)
        snapshots.append(
            make(
                MetricName.FRESHNESS,
                age,
                contract.freshness_max_age_seconds,
                age <= contract.freshness_max_age_seconds,
                f_reason,
            )
        )

    baseline = store.latest(contract.name, MetricName.COMPLETENESS)
    drop, passed, d_reason = _drift(completeness, baseline, drift_max_drop)
    snapshots.append(
        make(MetricName.COMPLETENESS_DRIFT, drop, drift_max_drop, passed, d_reason)
    )

    for snapshot in snapshots:
        store.append(snapshot)
    return snapshots


def _completeness(contract: Contract, records: list[dict[str, Any]]) -> tuple[float, str]:
    total = len(records) * len(contract.fields)
    if total == 0:
        return 1.0, "空数据集：分母为 0，完整性按 1.0 计"
    filled = 0
    for row_index, record in enumerate(records):
        for spec in contract.fields:
            if spec.name in record and record[spec.name] is not None:
                filled += 1
    value = filled / total
    return value, (
        f"已填充单元格 {filled}/{total}（行 {len(records)} × 字段 {len(contract.fields)}）；"
        f"单元格出现且值非 null 计为完整"
    )


def _uniqueness(contract: Contract, records: list[dict[str, Any]]) -> tuple[float, str]:
    unique_fields = [f for f in contract.fields if f.constraints.unique]
    if not unique_fields:
        return 1.0, "契约未声明 unique 字段，唯一性按 1.0 计（无约束对象）"
    ratios: list[float] = []
    details: list[str] = []
    for spec in unique_fields:
        values = [r[spec.name] for r in records if spec.name in r and r[spec.name] is not None]
        ratio = (len(set(values)) / len(values)) if values else 1.0
        ratios.append(ratio)
        details.append(f"{spec.name}={len(set(values)) if values else 0}去重/{len(values)}非空")
    value = min(ratios)
    return value, "唯一字段去重率取最小值：" + "；".join(details)


def _freshness(
    contract: Contract, records: list[dict[str, Any]], now_epoch: float
) -> tuple[float, str]:
    newest: float | None = None
    source_field = ""
    for spec in contract.fields:
        if spec.type is not FieldType.TIMESTAMP:
            continue
        for record in records:
            if spec.name not in record:
                continue
            epoch = _to_epoch(record[spec.name])
            if epoch is not None and (newest is None or epoch > newest):
                newest = epoch
                source_field = spec.name
    if newest is None:
        return float("inf"), "数据中不存在可解析的 timestamp 字段，无法证明刷新时效"
    age = max(0.0, now_epoch - newest)
    return age, (
        f"以字段 '{source_field}' 的最大时间戳 {newest} 为刷新点，"
        f"age=now-refresh={age:.3f}s，阈值 {contract.freshness_max_age_seconds}s"
    )


def _drift(
    current: float,
    baseline: MetricSnapshot | None,
    drift_max_drop: float,
) -> tuple[float, bool, str]:
    if baseline is None:
        return 0.0, True, "首批数据，无历史完整性基线，漂移记 0 且直接通过"
    drop = baseline.value - current
    return drop, drop <= drift_max_drop, (
        f"相对上一批次 {baseline.batch_id} 的完整性 {baseline.value}，"
        f"本次 {current}，下降 {drop:.6f}，允许最大下降 {drift_max_drop}"
    )


def _to_epoch(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None
