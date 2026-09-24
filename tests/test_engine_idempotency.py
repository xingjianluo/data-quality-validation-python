"""并发与重复提交：结论一致、指标只累计一次；非法契约不留任何痕迹。"""

from __future__ import annotations

import threading

import pytest

from conftest import log_case
from dqv.engine import ValidationEngine
from dqv.errors import ContractRejectedError, DataValidationError, RejectReason

CONTRACT = {"name": "orders", "fields": [
    {"name": "id", "type": "integer", "constraints": {"unique": True, "min_value": 1}},
    {"name": "ts", "type": "timestamp"},
]}
RECORDS = [{"id": 1, "ts": 1000}, {"id": 2, "ts": 1000}]
SNAPSHOTS_PER_RUN = 3  # completeness + uniqueness + completeness_drift（无新鲜度阈值）


class TestConcurrentSubmission:
    def test_concurrent_same_key_runs_once(self):
        engine = ValidationEngine()
        results: list = []
        errors: list = []
        barrier = threading.Barrier(16)

        def worker():
            barrier.wait()
            try:
                results.append(engine.submit(CONTRACT, RECORDS, idempotency_key="k"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        log_case("concurrent", "16 线程同 idempotency_key=k",
                 "共享同一份结果，指标只累计一次(3 条快照)",
                 "leader 执行，follower 在 Event 上等待")
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 16
        assert len({r.signature() for r in results}) == 1
        assert len(engine.metrics.all_snapshots()) == SNAPSHOTS_PER_RUN
        log_case("concurrent-verdict", "16 submissions",
                 "1 unique signature, 3 snapshots",
                 {"snapshots": len(engine.metrics.all_snapshots())})

    def test_duplicate_submission_returns_same_result(self):
        engine = ValidationEngine()
        r1 = engine.submit(CONTRACT, RECORDS, idempotency_key="k")
        r2 = engine.submit(CONTRACT, RECORDS, idempotency_key="k")
        log_case("duplicate", "相同 key + 相同内容提交两次",
                 "signature 相同，指标仍为 3 条",
                 "缓存命中，不重新校验也不追加指标")
        assert r1.signature() == r2.signature()
        assert len(engine.metrics.all_snapshots()) == SNAPSHOTS_PER_RUN

    def test_same_key_different_payload_rejected(self):
        engine = ValidationEngine()
        engine.submit(CONTRACT, RECORDS, idempotency_key="k")
        log_case("key-collision", "key=k 但记录内容不同",
                 "DATA_VIOLATION/409，且指标不增加",
                 "幂等键绑定内容指纹，防止把新执行伪装成重复执行")
        with pytest.raises(DataValidationError):
            engine.submit(CONTRACT, [{"id": 9, "ts": 1000}], idempotency_key="k")
        assert len(engine.metrics.all_snapshots()) == SNAPSHOTS_PER_RUN

    def test_fingerprint_dedup_without_key(self):
        engine = ValidationEngine()
        engine.submit(CONTRACT, RECORDS)
        engine.submit(CONTRACT, RECORDS)
        log_case("fingerprint", "无 key，相同契约+数据两次",
                 "按内容指纹去重，指标 3 条", "fingerprint=sha256(canonical json)[:16]")
        assert len(engine.metrics.all_snapshots()) == SNAPSHOTS_PER_RUN

    def test_malformed_contract_leaves_no_trace(self):
        engine = ValidationEngine()
        with pytest.raises(ContractRejectedError) as exc:
            engine.submit({"name": "bad", "fields": []}, [], idempotency_key="bad")
        log_case("malformed-in-engine", "fields=[]",
                 "MALFORMED_CONTRACT，无批次、无指标、键被清理",
                 "执行前拒绝，失败路径移除 slot")
        assert exc.value.reason is RejectReason.MALFORMED_CONTRACT
        assert engine.metrics.all_snapshots() == []
        assert engine.get_result("bad") is None

    def test_different_keys_count_independently(self):
        engine = ValidationEngine()
        engine.submit(CONTRACT, RECORDS, idempotency_key="a")
        engine.submit(CONTRACT, RECORDS, idempotency_key="b")
        log_case("different-keys", "key=a 与 key=b 同内容",
                 "各自独立执行，指标 6 条", "不同标识即不同批次")
        assert len(engine.metrics.all_snapshots()) == SNAPSHOTS_PER_RUN * 2
