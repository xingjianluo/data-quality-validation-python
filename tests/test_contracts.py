"""畸形契约 / 冲突约束必须被拒绝，且错误类别与原因可区分。"""

from __future__ import annotations

import pytest

from app.quality.contracts import normalize_contract
from app.quality.errors import (
    ConstraintConflictError,
    ContractInvalidError,
    ErrorCategory,
    FieldNotDeclaredError,
)


def reject(raw):
    with pytest.raises(Exception) as info:
        normalize_contract(raw)
    return info.value


def test_reject_non_object(case):
    raw = ["not", "an", "object"]
    case.input("raw", raw)
    case.expect((ErrorCategory.CONTRACT_INVALID, "contract_not_object"))
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    case.actual(err.category.value)
    assert isinstance(err, ContractInvalidError)
    assert err.reason == "contract_not_object"


def test_reject_blank_name(case):
    raw = {"name": "   ", "fields": [{"name": "id", "type": "integer"}]}
    case.input("raw", raw)
    case.expect("contract_name_blank")
    err = reject(raw)
    case.basis(err.reason)
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "contract_name_blank"


def test_reject_empty_fields(case):
    raw = {"name": "c", "fields": []}
    case.input("raw", raw)
    case.expect("fields_empty")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "fields_empty"
    assert err.category == ErrorCategory.CONTRACT_INVALID


def test_reject_unknown_type(case):
    raw = {"name": "c", "fields": [{"name": "id", "type": "struct"}]}
    case.input("raw", raw)
    case.expect("field_definition_invalid (依赖解析错误归一化)")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.reason == "field_definition_invalid"
    # pydantic 原始异常不得透出为异常类型，仅作为 cause 留存。
    assert err.__cause__ is not None


def test_reject_field_name_blank(case):
    raw = {"name": "c", "fields": [{"name": "", "type": "integer"}]}
    case.input("raw", raw)
    case.expect("field_name_blank")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "field_name_blank"


def test_reject_duplicate_field_as_conflict(case):
    raw = {
        "name": "c",
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "id", "type": "string"},
        ],
    }
    case.input("raw", raw)
    case.expect((ErrorCategory.CONSTRAINT_CONFLICT, "duplicate_field"))
    err = reject(raw)
    case.basis((err.category.value, err.reason, err.field))
    assert isinstance(err, ConstraintConflictError)
    assert err.reason == "duplicate_field"


def test_reject_min_greater_than_max(case):
    raw = {
        "name": "c",
        "fields": [{"name": "n", "type": "integer", "min_value": 9, "max_value": 1}],
    }
    case.input("raw", raw)
    case.expect("min_gt_max_value")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert isinstance(err, ConstraintConflictError)
    assert err.reason == "min_gt_max_value"


def test_reject_min_length_greater_than_max_length(case):
    raw = {
        "name": "c",
        "fields": [
            {"name": "s", "type": "string", "min_length": 8, "max_length": 3}
        ],
    }
    case.input("raw", raw)
    case.expect("min_gt_max_length")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "min_gt_max_length"


def test_reject_length_on_non_string(case):
    raw = {"name": "c", "fields": [{"name": "n", "type": "integer", "min_length": 1}]}
    case.input("raw", raw)
    case.expect("length_on_non_string")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "length_on_non_string"


def test_reject_empty_allowed(case):
    raw = {"name": "c", "fields": [{"name": "g", "type": "string", "allowed": []}]}
    case.input("raw", raw)
    case.expect("allowed_empty (空白名单与必填冲突)")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert isinstance(err, ConstraintConflictError)
    assert err.reason == "allowed_empty"


def test_reject_allowed_below_min(case):
    raw = {
        "name": "c",
        "fields": [
            {"name": "g", "type": "integer", "min_value": 5, "allowed": [1, 2]}
        ],
    }
    case.input("raw", raw)
    case.expect("allowed_below_min")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert isinstance(err, ConstraintConflictError)
    assert err.reason == "allowed_below_min"


def test_reject_allowed_type_mismatch(case):
    raw = {
        "name": "c",
        "fields": [{"name": "g", "type": "integer", "allowed": ["a"]}],
    }
    case.input("raw", raw)
    case.expect("allowed_type_mismatch")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "allowed_type_mismatch"


def test_reject_freshness_field_undeclared(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "freshness_field": "ts",
    }
    case.input("raw", raw)
    case.expect((ErrorCategory.FIELD_NOT_DECLARED, "freshness_field_undeclared"))
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert isinstance(err, FieldNotDeclaredError)
    assert err.reason == "freshness_field_undeclared"


def test_reject_freshness_field_wrong_type(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "freshness_field": "id",
        "freshness_max_age_seconds": 10,
    }
    case.input("raw", raw)
    case.expect("freshness_field_wrong_type")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "freshness_field_wrong_type"


def test_reject_negative_freshness_sla(case):
    raw = {
        "name": "c",
        "fields": [{"name": "ts", "type": "timestamp"}],
        "freshness_field": "ts",
        "freshness_max_age_seconds": -1,
    }
    case.input("raw", raw)
    case.expect("freshness_max_age_negative")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.reason == "freshness_max_age_negative"


def test_valid_contract_normalizes(case):
    raw = {
        "name": "c",
        "fields": [
            {"name": "id", "type": "integer", "unique": True},
            {"name": "ts", "type": "timestamp"},
        ],
        "freshness_field": "ts",
        "freshness_max_age_seconds": 60,
    }
    case.input("raw", raw)
    case.expect("归一化成功，2 字段，freshness 绑定 ts")
    contract = normalize_contract(raw)
    case.basis(([s.name for s in contract.fields_], contract.freshness_field))
    assert contract.get("id").unique is True
    assert contract.freshness_field == "ts"


def test_reject_unknown_top_level_option(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        # 拼错的契约级配置项，绝不能被悄悄放过。
        "freshness_filed": "id",
    }
    case.input("raw", raw)
    case.expect((ErrorCategory.CONTRACT_INVALID, "contract_unknown_option"))
    err = reject(raw)
    case.basis((err.category.value, err.reason, err.message))
    assert isinstance(err, ContractInvalidError)
    assert err.reason == "contract_unknown_option"
    assert "freshness_filed" in err.message


def test_reject_unknown_field_level_option(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer", "minn_value": 1}],
    }
    case.input("raw", raw)
    case.expect((ErrorCategory.CONTRACT_INVALID, "field_unknown_option"))
    err = reject(raw)
    case.basis((err.category.value, err.reason, err.field, err.message))
    assert isinstance(err, ContractInvalidError)
    assert err.reason == "field_unknown_option"
    assert err.field == "id"
    assert "minn_value" in err.message


def test_reject_misspelled_constraint_name(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer", "unqiue": True}],
    }
    case.input("raw", raw)
    case.expect("约束名拼错(unqiue)按 field_unknown_option 拒绝")
    err = reject(raw)
    case.basis((err.reason, err.field, err.message))
    assert err.reason == "field_unknown_option"
    assert err.field == "id"
    assert "unqiue" in err.message


def test_reject_drift_tolerance_out_of_range(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "drift_tolerance": 1.5,
    }
    case.input("raw", raw)
    case.expect("drift_tolerance_out_of_range")
    err = reject(raw)
    case.basis((err.category.value, err.reason))
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "drift_tolerance_out_of_range"
