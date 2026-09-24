"""校验执行：违约项分类正确、可解释；依赖故障与数据违约必须可区分。"""

from __future__ import annotations

import pytest

from conftest import log_case
from dqv.contracts import parse_contract
from dqv.errors import DependencyFailureError, ErrorCategory
from dqv.validators import validate_records


@pytest.fixture()
def contract():
    return parse_contract({"name": "orders", "fields": [
        {"name": "id", "type": "integer",
         "constraints": {"unique": True, "min_value": 1}},
        {"name": "name", "type": "string",
         "constraints": {"min_length": 2, "max_length": 8, "pattern": "^[a-z]+$"}},
        {"name": "role", "type": "string", "constraints": {
            "required": False, "nullable": True, "allowed_values": ["admin", "user"]}},
    ]})


class TestViolationClassification:
    def test_each_violation_has_code_expected_actual_basis(self, contract):
        records = [
            {"id": 1, "name": "ab"},
            {"id": 0, "name": "X", "role": "root"},
            {"id": 1, "name": None},
            {"name": "ab"},
        ]
        result = validate_records(contract, records, "batch-1")
        codes = sorted({v.code.value for v in result.violations})
        log_case("violations", records,
                 "codes={value_out_of_range,length_out_of_range,"
                 "not_in_allowed_values,null_not_allowed,required_missing,duplicate_key}",
                 codes)
        assert result.passed is False
        expected_codes = {
            "value_out_of_range", "length_out_of_range", "not_in_allowed_values",
            "null_not_allowed", "required_missing", "duplicate_key",
        }
        assert set(codes) == expected_codes
        for v in result.violations:
            assert v.expected and v.actual and v.basis
            log_case(f"violation-{v.code.value}",
                     {"row": v.row_index, "field": v.field_name, "actual": v.actual},
                     v.expected, v.basis)

    def test_clean_data_passes(self, contract):
        records = [{"id": 1, "name": "abc", "role": "admin"},
                   {"id": 2, "name": "zzz"}]
        result = validate_records(contract, records, "batch-2")
        log_case("clean", records, "passed=True", "全部字段满足声明类型与约束")
        assert result.passed is True and result.violations == ()


class TestErrorCategoryDistinction:
    def test_dependency_failure_is_not_data_violation(self, contract):
        class ExplodingBackend:
            component = "exploder"

            def check(self, spec, value):
                raise RuntimeError("secrets of internal dependency: /tmp/x")

        records = [{"id": 1, "name": "ab"}]
        log_case("dependency-boom", "RuntimeError from checker backend",
                 "DEPENDENCY_FAILURE（原始异常与内部细节不得透出）",
                 "边界捕获并归一化")
        with pytest.raises(DependencyFailureError) as exc:
            validate_records(contract, records, "batch-3", backend=ExplodingBackend())
        err = exc.value
        assert err.category is ErrorCategory.DEPENDENCY_FAILURE
        assert "secrets" not in err.message and "RuntimeError" not in err.message
        assert err.component == "exploder"
        log_case("dependency-normalized", "exploder.check raises",
                 err.category.value, err.to_dict())

    def test_data_violation_does_not_become_dependency_failure(self, contract):
        records = [{"id": "not-an-int", "name": "ab"}]
        result = validate_records(contract, records, "batch-4")
        log_case("type-mismatch", records, "DATA_VIOLATION/type_mismatch",
                 "默认后端返回 False，不抛异常，结论随 ValidationResult 返回")
        assert result.passed is False
        assert result.violations[0].code.value == "type_mismatch"
