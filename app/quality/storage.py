"""线程安全的本地历史留存。

按 ``(contract, batch_id)`` 幂等去重：重复 / 并发提交只有第一个线程能写入，
指标也只累计一次。所有读路径返回拷贝，避免外部突变历史。
"""

from __future__ import annotations

from threading import Lock

from app.quality.drift import DriftVerdict
from app.quality.engine import ValidationResult
from app.quality.metrics import MetricSnapshot


class HistoryStore:
    """进程内留存：校验结果 + 指标快照 + 漂移结论。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._results: dict[tuple[str, str], ValidationResult] = {}
        self._metrics: dict[tuple[str, str], list[MetricSnapshot]] = {}
        self._verdicts: dict[tuple[str, str], DriftVerdict] = {}
        # 每个契约按首次判定落库顺序记录批次，用于"最近一次已判定批次"基线。
        self._order: dict[str, list[str]] = {}

    @staticmethod
    def _key(contract: str, batch_id: str) -> tuple[str, str]:
        return (contract, batch_id)

    def save_result_if_absent(
        self, contract: str, batch_id: str, result: ValidationResult
    ) -> bool:
        """首次写入返回 True；批次已存在返回 False（调用方据此跳过累计）。"""

        key = self._key(contract, batch_id)
        with self._lock:
            if key in self._results:
                return False
            self._results[key] = result
            return True

    def commit_if_absent(
        self,
        contract: str,
        batch_id: str,
        result: ValidationResult,
        snapshots: list[MetricSnapshot],
        verdict: DriftVerdict | None,
    ) -> bool:
        """原子提交首批次的全部产物。

        结果、指标、漂移结论与批次顺序在同一把锁内一起落库；
        批次已存在则整体不写，返回 False。保证并发下只判定、累计一次。
        """

        key = self._key(contract, batch_id)
        with self._lock:
            if key in self._results:
                return False
            self._results[key] = result
            # 固化为独立列表，外部后续修改不影响历史。
            self._metrics[key] = list(snapshots)
            if verdict is not None:
                self._verdicts[key] = verdict
            self._order.setdefault(contract, []).append(batch_id)
            return True

    def get_result(self, contract: str, batch_id: str) -> ValidationResult | None:
        with self._lock:
            return self._results.get(self._key(contract, batch_id))

    def append_metrics_if_absent(
        self,
        contract: str,
        batch_id: str,
        snapshots: list[MetricSnapshot],
    ) -> bool:
        """仅在该批次尚未留存指标时写入；已存在则整批忽略。"""

        key = self._key(contract, batch_id)
        with self._lock:
            if key in self._metrics:
                return False
            # 固化为独立列表，外部后续修改不影响历史。
            self._metrics[key] = list(snapshots)
            return True

    def get_metrics(self, contract: str, batch_id: str) -> list[MetricSnapshot]:
        with self._lock:
            return list(self._metrics.get(self._key(contract, batch_id), []))

    def get_verdict(self, contract: str, batch_id: str) -> DriftVerdict | None:
        with self._lock:
            return self._verdicts.get(self._key(contract, batch_id))

    def has_batch(self, contract: str, batch_id: str) -> bool:
        """该批次是否已完成首次判定并落库。"""

        with self._lock:
            return self._key(contract, batch_id) in self._results

    def previous_batch(self, contract: str, batch_id: str) -> str | None:
        """判定顺序上紧邻当前批次之前、且已判定的批次（最近基线）。

        并发下当前批次可能尚未登记进顺序，因此按顺序表中排在它之前的
        最后一个批次取值；若它尚未登记，则取目前最后一个已判定批次。
        """

        with self._lock:
            order = self._order.get(contract, [])
            if batch_id in order:
                idx = order.index(batch_id)
                return order[idx - 1] if idx > 0 else None
            return order[-1] if order else None

    def list_batches(self, contract: str) -> list[str]:
        with self._lock:
            return sorted(
                batch_id for (cname, batch_id) in self._results if cname == contract
            )
