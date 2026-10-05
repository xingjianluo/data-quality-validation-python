"""门面服务：契约注册、校验提交（幂等）、指标判定、基线漂移与回溯。

并发/重复提交语义：

* 以 ``(contract, batch_id)`` 为幂等键；
* 同一批次首次提交真正执行校验并累计一次指标、判定一次漂移；
* 并发线程通过 per-batch 锁 + double-check 串行化，后来者直接拿到
  首次结论，绝不重复累计指标、重复判定漂移；
* 阈值 / 漂移容忍幅度在"批次首次判定时"读取并固化进快照与结论，之后
  注册同名契约的新版本不会回写历史批次；
* 基线选择在契约级判定锁内完成，保证"最近一次已判定批次"这一基线
  对并发的不同批次也是确定、不重样的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.quality.contracts import Contract, normalize_contract
from app.quality.drift import (
    BaselineMode,
    DriftVerdict,
    evaluate_drift,
)
from app.quality.engine import ValidationEngine, ValidationResult
from app.quality.errors import (
    FieldNotDeclaredError,
    MetricNotFoundError,
)
from app.quality.metrics import MetricSnapshot, completeness, freshness, uniqueness
from app.quality.storage import HistoryStore


class QualityService:
    """编排契约校验 + 指标历史 + 基线漂移的应用服务。"""

    def __init__(self, engine: ValidationEngine | None = None) -> None:
        self._contracts: dict[str, Contract] = {}
        self._store = HistoryStore()
        self._engine = engine or ValidationEngine()
        # 契约注册表与批次执行锁各自独立，避免跨契约互斥。
        self._register_lock = Lock()
        self._batch_locks: dict[tuple[str, str], Lock] = {}
        self._batch_locks_guard = Lock()
        # 契约级判定锁：串行化基线解析 + 首次落库（锁序固定为
        # 批次锁 -> 契约判定锁，不会反向获取，故无死锁）。
        self._judge_locks: dict[str, Lock] = {}
        self._judge_locks_guard = Lock()

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

    def _get_judge_lock(self, contract: str) -> Lock:
        with self._judge_locks_guard:
            lock = self._judge_locks.get(contract)
            if lock is None:
                lock = Lock()
                self._judge_locks[contract] = lock
            return lock

    def submit(
        self,
        contract_name: str,
        batch_id: str,
        rows: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        baseline_batch_id: str | None = None,
    ) -> ValidationResult:
        """幂等提交：重复/并发批次返回首次结论，且不重复累计/判定。

        ``baseline_batch_id`` 显式指定漂移基线；不传则以判定顺序上最近的
        已判定批次为基线。漂移结论只在批次首次判定时计算并固化。
        显式基线不存在 / 尚未判定 / 指向当前批次时，以可区分原因拒绝，
        不会硬凑结论（且不产生任何落库，调用方可改正后重试同一批次）。
        """

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
            snapshots = self._evaluate(contract, batch_id, rows, evaluated_now)

            verdict: DriftVerdict | None = None
            # 基线解析 + 漂移判定 + 落库在契约级锁内串行：
            # 不同批次的"最近基线"互不抢占、确定有序。
            judge_lock = self._get_judge_lock(contract_name)
            with judge_lock:
                # triple-check：进入契约锁前，当前批次可能已被同批次的
                # 其它线程提交完成。
                if self._store.has_batch(contract_name, batch_id):
                    return self._store.get_result(contract_name, batch_id)  # type: ignore[return-value]

                baseline_id, mode = self._resolve_baseline(
                    contract_name, batch_id, baseline_batch_id
                )
                if baseline_id is not None:
                    baseline_snaps = self._store.get_metrics(
                        contract_name, baseline_id
                    )
                    verdict = evaluate_drift(
                        contract=contract_name,
                        batch_id=batch_id,
                        current=snapshots,
                        baseline_batch_id=baseline_id,
                        baseline=baseline_snaps,
                        tolerance=contract.drift_tolerance,
                        mode=mode,
                        evaluated_at=evaluated_now,
                    )

                # 结果、指标、漂移结论原子落库；并发下只有首个成功。
                self._store.commit_if_absent(
                    contract_name, batch_id, result, snapshots, verdict
                )
            return result

    def _resolve_baseline(
        self,
        contract_name: str,
        batch_id: str,
        explicit: str | None,
    ) -> tuple[str | None, BaselineMode]:
        """确定基线批次。

        * 显式指定：批次必须存在且已判定，且不能是当前批次，否则拒绝；
        * 未指定：取判定顺序上最近的前一个批次，没有则返回 None
          （首个批次无基线，漂移结论缺省，不硬凑）。
        """

        if explicit is not None:
            if explicit == batch_id:
                raise MetricNotFoundError(
                    f"契约 {contract_name} 的批次 {batch_id} 不能以自身为漂移基线",
                    contract=contract_name,
                    batch=batch_id,
                    reason="baseline_is_current_batch",
                )
            if not self._store.has_batch(contract_name, explicit):
                # 不区分"从未提交"与"提交了但还没判定完"，对调用方都是
                # 同一个明确拒绝原因：基线不可用。
                raise MetricNotFoundError(
                    f"指定的基线批次 {explicit} 不存在或尚未判定，"
                    f"无法为批次 {batch_id} 判定漂移",
                    contract=contract_name,
                    batch=explicit,
                    reason="baseline_batch_not_judged",
                )
            return explicit, BaselineMode.EXPLICIT

        latest = self._store.previous_batch(contract_name, batch_id)
        if latest is None:
            return None, BaselineMode.LATEST
        return latest, BaselineMode.LATEST

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
        verdict = self._store.get_verdict(contract_name, batch_id)
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
            # 新增信息仅追加：首个批次（无基线）时为 None，不改变老字段含义。
            "drift": verdict.trace() if verdict is not None else None,
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
