"""数据违规 vs 依赖故障必须映射为不同类别；重复执行结论一致。"""

from __future__ import annotations

import pytest

from app.quality.contracts import normalize_contract
from app.quality.engine import ValidationEngine
from app.quality.errors import DependencyFailureError, ErrorCategory


@pytest.fixture
def contract():
    return normalize_contract(
        {
            "name": "c",
            "fields": [
                {"name": "id", "type": "integer", "unique": True},
                {
                    "name": "age",
                    "type": "integer",
                    "min_value": 0,
                    "max_value": 120,
                    "required": False,
                    "nullable": True,
                },
                {"name": "role", "type": "string", "allowed": ["a", "b"]},
            ],
        }
    )


def test_data_violation_categories(contract, case):
    rows = [
        {"id": 1, "age": 30, "role": "a"},
        {"id": "x", "age": 200, "role": "z"},   # 类型/越界/枚举三类违规
    ]
    case.input("rows", rows)
    case.expect("type_mismatch / max_value / allowed 均为 data_violation")
    result = ValidationEngine().validate(contract, rows, "B1")
    rules = {(v.field, v.rule, v.category) for v in result.violations}
    case.basis(rules)
    assert ("id", "type_mismatch", "data_violation") in rules
    assert ("age", "max_value", "data_violation") in rules
    assert ("role", "allowed", "data_violation") in rules
    assert result.passed is False


def test_missing_field_is_distinct_category(contract, case):
    rows = [{"id": 1, "age": 30}]  # 缺必填 role
    case.input("rows", rows)
    case.expect("role -> data_missing_field，区别于 data_violation")
    result = ValidationEngine().validate(contract, rows, "B1")
    case.basis([(v.field, v.category) for v in result.violations])
    assert result.violations[0].category == "data_missing_field"
    assert result.violations[0].rule == "missing"


def test_nullable_optional_allowed(contract, case):
    rows = [
        {"id": 2, "age": None, "role": "a"},   # nullable 放行
        {"id": 3, "role": "b"},                # 可选 age 缺列放行
    ]
    case.input("rows", rows)
    case.expect("passed=True")
    result = ValidationEngine().validate(contract, rows, "B1")
    case.basis(result.violation_count)
    assert result.passed is True


def test_unique_violation_is_data_violation(contract, case):
    rows = [
        {"id": 1, "role": "a"},
        {"id": 1, "role": "b"},
    ]
    case.input("rows", rows)
    case.expect("第二行 id 触发 unique / data_violation")
    result = ValidationEngine().validate(contract, rows, "B1")
    uniq = [v for v in result.violations if v.rule == "unique"]
    case.basis([(v.row, v.field, v.category) for v in uniq])
    assert len(uniq) == 1
    assert uniq[0].category == "data_violation"


def test_repeated_execution_is_deterministic(contract, case):
    rows = [{"id": 1, "age": -5, "role": "a"}]
    case.input("rows", rows)
    case.expect("两次 validate 结果完全相等")
    engine = ValidationEngine()
    r1 = engine.validate(contract, rows, "B1")
    r2 = engine.validate(contract, rows, "B1")
    case.basis((r1.model_dump() == r2.model_dump(), r1.violations[0].rule))
    assert r1 == r2


def test_dependency_failure_is_normalized(contract, case):
    class BrokenChecker:
        def check(self, spec, value):
            raise RuntimeError("SECRET-INTERNAL-STACK")

    rows = [{"id": 1, "age": 30, "role": "a"}]
    case.input("rows", rows)
    case.expect(ErrorCategory.DEPENDENCY_FAILURE)
    with pytest.raises(DependencyFailureError) as info:
        ValidationEngine(BrokenChecker()).validate(contract, rows, "B9")
    err = info.value
    case.basis((err.category.value, err.reason, err.cause is not None))
    assert err.category == ErrorCategory.DEPENDENCY_FAILURE
    assert "SECRET-INTERNAL-STACK" not in err.message  # 不直接透出
    assert isinstance(err.__cause__, RuntimeError)


def test_data_and_dependency_categories_differ(contract, case):
    case.expect("DATA_VIOLATION != DEPENDENCY_FAILURE")
    case.basis(
        (ErrorCategory.DATA_VIOLATION.value,
         ErrorCategory.DATA_MISSING_FIELD.value,
         ErrorCategory.DEPENDENCY_FAILURE.value)
    )
    assert ErrorCategory.DATA_VIOLATION != ErrorCategory.DEPENDENCY_FAILURE
    assert ErrorCategory.DATA_MISSING_FIELD != ErrorCategory.DEPENDENCY_FAILURE
