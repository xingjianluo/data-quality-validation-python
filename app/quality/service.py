"""门面服务：契约注册、校验提交（幂等）、指标判定与回溯。

并发/重复提交语义：

* 以 ``(contract, batch_id)`` 为幂等键；
* 同一批次首次提交真正执行校验并累计一次指标；
* 并发线程通过 per-batch 锁 + double-check 串行化，后来者直接拿到
  首次结论，绝不重复累计指标；
* 阈值在"批次首次判定时"读取并固化进快照，之后注册同名契约的新阈值
  不会回写历史批次。
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.quality.contracts import Contract, normalize_contract
from app.quality.engine import ValidationEngine, ValidationResult
from app.quality.errors import (
    ContractInvalidError,
    FieldNotDeclaredError,
    MetricNotFoundError,
)
from app.quality.metrics import MetricSnapshot, completeness, freshness, uniqueness
from app.quality.storage import HistoryStore


class QualityService:
    """编排契约校验 + 指标历史的应用服务。"""

    def __init__(self, engine: ValidationEngine | None = None) -> None:
        self._contracts: dict[str, Contract] = {}
        self._store = HistoryStore()
        self._engine = engine or ValidationEngine()
        # 契约注册表与批次执行锁各自独立，避免跨契约互斥。
        self._register_lock = Lock()
        self._batch_locks: dict[tuple[str, str], Lock] = {}
        self._batch_locks_guard = Lock()

    def register_contract(self, raw: dict[str, Any]) -> Contract:
        """归一化并注册契约；非法契约直接以可区分错误拒绝。"""

        contract = normalize_contract(raw)
        with self._register_lock:
            self._contracts[contract.name] = contract
        return contract

    def _get_contract(self, name: str) -> Contract:
        try:
            return self._contracts[name]
        except KeyError:
            raise FieldNotDeclaredError(
                f"契约 {name} 尚未注册",
                contract=name,
                reason="contract_not_registered",
            ) from None

    def _get_batch_lock(self, contract: str, batch_id: str) -> Lock:
        key = (contract, batch_id)
        with self._batch_locks_guard:
            lock = self._batch_locks.get(key)
            if lock is None:
                lock = Lock()
                self._batch_locks[key] = lock
            return lock

    def submit(
        self,
        contract_name: str,
        batch_id: str,
        rows: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> ValidationResult:
        """幂等提交：重复/并发批次返回首次结论，且不重复累计指标。"""

        contract = self._get_contract(contract_name)
        evaluated_now = now or self._utcnow()

        # 快路径：已有结论直接返回（不持锁，减少争用）。
        existing = self._store.get_result(contract_name, batch_id)
        if existing is not None:
            return existing

        lock = self._get_batch_lock(contract_name, batch_id)
        with lock:
            # double-check：拿到锁期间可能已被其它线程执行。
            existing = self._store.get_result(contract_name, batch_id)
            if existing is not None:
                return existing

            result = self._engine.validate(contract, rows, batch_id)
            # 结果与指标在同一临界区内落库；指标只在首次写入时累计。
            stored = self._store.save_result_if_absent(
                contract_name, batch_id, result
            )
            if stored:
                self._store.append_metrics_if_absent(
                    contract_name, batch_id,
                    self._evaluate(contract, batch_id, rows, evaluated_now),
                )
            return result

    def _evaluate(
        self,
        contract: Contract,
        batch_id: str,
        rows: list[dict[str, Any]],
        now: datetime,
    ) -> list[MetricSnapshot]:
        """按当前契约阈值计算三项指标（阈值在此刻固化）。"""

        evaluated_at = now
        required_fields = [s.name for s in contract.fields_ if s.required]

        snapshots: list[MetricSnapshot] = [
            completeness(
                batch_id,
                contract.name,
                rows,
                required_fields,
                threshold=1.0,
                evaluated_at=evaluated_at,
            )
        ]

        unique_fields = [s.name for s in contract.fields_ if s.unique]
        for key_field in unique_fields:
            snapshots.append(
                uniqueness(
                    batch_id,
                    contract.name,
                    rows,
                    key_field,
                    threshold=1.0,
                    evaluated_at=evaluated_at,
                )
            )

        if contract.freshness_field and contract.freshness_max_age_seconds is not None:
            snapshots.append(
                freshness(
                    batch_id,
                    contract.name,
                    rows,
                    contract.freshness_field,
                    contract.freshness_max_age_seconds,
                    now,
                    evaluated_at,
                )
            )
        return snapshots

    def trace(self, contract_name: str, batch_id: str) -> dict[str, Any]:
        """从指标回溯到契约、批次与触发原因。"""

        self._get_contract(contract_name)
        result = self._store.get_result(contract_name, batch_id)
        if result is None:
            raise MetricNotFoundError(
                f"契约 {contract_name} 的批次 {batch_id} 不存在",
                contract=contract_name,
                batch=batch_id,
                reason="batch_not_found",
            )
        snapshots = self._store.get_metrics(contract_name, batch_id)
        return {
            "contract": contract_name,
            "batch_id": batch_id,
            "validation": {
                "passed": result.passed,
                "total_rows": result.total_rows,
                "violation_count": result.violation_count,
                "violations": [v.model_dump() for v in result.violations],
            },
            "metrics": [s.trace() for s in snapshots],
        }

    def history(self, contract_name: str) -> list[MetricSnapshot]:
        """返回契约下全部批次的指标快照（按批次有序、不可变）。"""

        self._get_contract(contract_name)
        out: list[MetricSnapshot] = []
        for batch_id in self._store.list_batches(contract_name):
            out.extend(self._store.get_metrics(contract_name, batch_id))
        return out

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
