"""线程安全的本地历史留存。

按 ``(contract, batch_id)`` 幂等去重：重复 / 并发提交只有第一个线程能写入，
指标也只累计一次。所有读路径返回拷贝，避免外部突变历史。
"""

from __future__ import annotations

from threading import Lock

from app.quality.drift import DriftReport
from app.quality.engine import ValidationResult
from app.quality.metrics import MetricSnapshot


class HistoryStore:
    """进程内留存：校验结果 + 指标快照 + 漂移判定报告。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._results: dict[tuple[str, str], ValidationResult] = {}
        self._metrics: dict[tuple[str, str], list[MetricSnapshot]] = {}
        self._drift: dict[tuple[str, str], DriftReport] = {}

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

    def save_drift_if_absent(
        self, contract: str, batch_id: str, report: DriftReport
    ) -> bool:
        """漂移报告同样只在批次首次判定时写入一次，之后不可改写。"""

        key = self._key(contract, batch_id)
        with self._lock:
            if key in self._drift:
                return False
            self._drift[key] = report
            return True

    def get_drift(self, contract: str, batch_id: str) -> DriftReport | None:
        with self._lock:
            return self._drift.get(self._key(contract, batch_id))

    def latest_batch(self, contract: str) -> str | None:
        """该契约最近一次判定的批次（按判定先后，非批次名排序）。"""

        with self._lock:
            for cname, batch_id in reversed(list(self._results)):
                if cname == contract:
                    return batch_id
            return None

    def list_batches(self, contract: str) -> list[str]:
        with self._lock:
            return sorted(
                batch_id for (cname, batch_id) in self._results if cname == contract
            )
