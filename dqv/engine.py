"""校验执行引擎：幂等去重、并发一致、指标只累计一次。

语义：
* 每个提交由幂等键标识（显式 idempotency_key > batch_id > 内容指纹）。
* 同一幂等键的并发提交：仅第一个线程真正执行，其余线程等待并共享
  同一份 ValidationResult，因此结论一致、指标只累计一次。
* 同一幂等键的重复提交：直接返回首次结果，不会再次校验或追加指标。
* 契约非法时在执行前抛出 ContractRejectedError，不产生任何批次与指标。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from dqv.contracts import Contract, parse_contract
from dqv.errors import ContractRejectedError, DataValidationError
from dqv.metrics import MetricsStore
from dqv.validators import ValidationResult, validate_records


@dataclass(frozen=True)
class _Slot:
    event: threading.Event
    batch_id: str
    fingerprint: str
    result: ValidationResult | None = None


class ValidationEngine:
    def __init__(self, completeness_min: float = 1.0, uniqueness_min: float = 1.0,
                 drift_max_drop: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self.metrics = MetricsStore()
        self._completeness_min = completeness_min
        self._uniqueness_min = uniqueness_min
        self._drift_max_drop = drift_max_drop

    def submit(
        self,
        contract: Contract | dict[str, Any] | str,
        records: list[dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        batch_id: str | None = None,
        now_epoch: float | None = None,
    ) -> ValidationResult:
        """提交一次校验；相同 key 的并发/重复提交只执行一次，结果一致。"""
        parsed = contract if isinstance(contract, Contract) else parse_contract(contract)

        fingerprint = _content_fingerprint(parsed, records)
        # 单一标识：既是幂等去重键，也是执行批次 ID，避免“去重键不同但
        # 批次相同”导致指标被重复累计。batch_id 优先，其次 idempotency_key。
        identifier = batch_id or idempotency_key or fingerprint
        effective_batch = identifier
        key = identifier

        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = _Slot(event=threading.Event(), batch_id=effective_batch, fingerprint=fingerprint)
                self._slots[key] = slot
                leader = True
            else:
                leader = False
                if slot.fingerprint != fingerprint:
                    raise DataValidationError(
                        f"批次 {effective_batch} 已绑定不同内容的提交；"
                        f"重复执行必须与首次提交内容完全一致"
                    )

        if not leader:
            slot.event.wait()
            cached = self._slots[key].result
            assert cached is not None, "等待结束后结果必须已就绪"
            return cached

        try:
            result = validate_records(parsed, records, effective_batch)
            from dqv.metrics import evaluate_quality

            evaluate_quality(
                parsed,
                records,
                effective_batch,
                now_epoch=now_epoch,
                store=self.metrics,
                completeness_min=self._completeness_min,
                uniqueness_min=self._uniqueness_min,
                drift_max_drop=self._drift_max_drop,
            )
        except ContractRejectedError:
            with self._lock:
                self._slots.pop(key, None)
            slot.event.set()
            raise
        except Exception:
            with self._lock:
                self._slots.pop(key, None)
            slot.event.set()
            raise

        with self._lock:
            self._slots[key] = _Slot(
                event=slot.event, batch_id=effective_batch,
                fingerprint=fingerprint, result=result,
            )
        slot.event.set()
        return result

    def get_result(self, batch_id: str) -> ValidationResult | None:
        with self._lock:
            for slot in self._slots.values():
                if slot.batch_id == batch_id:
                    return slot.result
        return None


def _content_fingerprint(contract: Contract, records: list[dict[str, Any]]) -> str:
    payload = {
        "contract": {
            "name": contract.name,
            "version": contract.version,
            "fields": [
                {"name": f.name, "type": f.type.value, "constraints": repr(f.constraints)}
                for f in contract.fields
            ],
            "freshness_max_age_seconds": contract.freshness_max_age_seconds,
        },
        "records": records,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return f"h-{digest[:16]}"
