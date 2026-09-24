"""畸形契约 / 冲突约束 / 字段缺失：三类拒绝原因必须可区分，绝不静默跳过。"""

from __future__ import annotations

import pytest

from conftest import log_case
from dqv.contracts import parse_contract
from dqv.errors import ContractRejectedError, ErrorCategory, RejectReason


class TestMalformedContracts:
    @pytest.mark.parametrize(
        "raw",
        [
            {"name": "c", "fields": []},
            {"name": "c", "fields": "not-a-list"},
            {"name": "c", "fields": [{"name": "a", "type": "unknown_type"}]},
            {"name": "c", "fields": [{"name": "a", "type": "string",
              "constraints": {"pattern": "("}}]},
            {"name": "c", "version": 0, "fields": [{"name": "a", "type": "string"}]},
            {"name": "c", "fields": [{"name": "a", "type": "integer",
              "constraints": {"min_length": 1}}]},
            "{not json",
            123,
        ],
    )
    def test_malformed_contract_rejected_with_reason(self, raw):
        log_case("malformed", raw, "MALFORMED_CONTRACT/422", "契约结构或约束定义不合法")
        with pytest.raises(ContractRejectedError) as exc:
            parse_contract(raw)
        err = exc.value
        assert err.category is ErrorCategory.CONTRACT_REJECTED
        assert err.reason is RejectReason.MALFORMED_CONTRACT
        assert err.details, "必须给出可区分的具体拒绝原因，不得静默跳过"
        log_case("malformed-verdict", raw, err.reason.value, err.details)

    def test_unknown_keys_are_malformed(self):
        raw = {"name": "c", "fields": [{"name": "a", "type": "string", "bogus": 1}]}
        log_case("unknown-key", raw, "MALFORMED_CONTRACT", "未知字段键不被静默忽略")
        with pytest.raises(ContractRejectedError) as exc:
            parse_contract(raw)
        assert exc.value.reason is RejectReason.MALFORMED_CONTRACT


class TestConflictingConstraints:
    @pytest.mark.parametrize(
        "raw",
        [
            {"name": "c", "fields": [{"name": "a", "type": "integer",
              "constraints": {"min_value": 10, "max_value": 1}}]},
            {"name": "c", "fields": [{"name": "a", "type": "string",
              "constraints": {"min_length": 5, "max_length": 2}}]},
            {"name": "c", "fields": [{"name": "a", "type": "integer",
              "constraints": {"allowed_values": [1, 2], "min_value": 10}}]},
            {"name": "c", "fields": [{"name": "a", "type": "string",
              "constraints": {"allowed_values": ["y"], "pattern": "^x$"}}]},
            {"name": "c", "fields": [
                {"name": "a", "type": "string"},
                {"name": "a", "type": "integer"}]},
        ],
    )
    def test_conflict_rejected_with_reason(self, raw):
        log_case("conflict", raw, "CONFLICTING_CONSTRAINTS/422", "约束组合为空集或字段重名")
        with pytest.raises(ContractRejectedError) as exc:
            parse_contract(raw)
        err = exc.value
        assert err.category is ErrorCategory.CONTRACT_REJECTED
        assert err.reason is RejectReason.CONFLICTING_CONSTRAINTS
        assert err.details
        log_case("conflict-verdict", raw, err.reason.value, err.details)


class TestMissingFields:
    @pytest.mark.parametrize(
        "raw",
        [
            {"name": "c"},
            {"name": "c", "fields": [{"type": "string"}]},
            {"name": "c", "fields": [{"name": "a"}]},
        ],
    )
    def test_missing_field_rejected_with_reason(self, raw):
        log_case("missing", raw, "MISSING_FIELD/422", "字段定义缺 name/type 或 fields 缺失")
        with pytest.raises(ContractRejectedError) as exc:
            parse_contract(raw)
        err = exc.value
        assert err.reason is RejectReason.MISSING_FIELD
        assert err.details
        log_case("missing-verdict", raw, err.reason.value, err.details)


class TestValidContracts:
    def test_valid_contract_normalized(self):
        raw = {"name": "orders", "version": 3, "fields": [
            {"name": "id", "type": "integer", "constraints": {"required": True, "unique": True}},
            {"name": "status", "type": "string", "constraints": {
                "nullable": True, "allowed_values": ("new", "paid"), "min_length": 0}}],
            "freshness_max_age_seconds": 60}
        c = parse_contract(raw)
        log_case("valid", raw, "accepted, constraints normalized",
                 f"fields={[f.name for f in c.fields]} freshness={c.freshness_max_age_seconds}")
        assert c.name == "orders" and c.version == 3
        assert c.fields[0].constraints.unique is True
        assert c.freshness_max_age_seconds == 60.0
