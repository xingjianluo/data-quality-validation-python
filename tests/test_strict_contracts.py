"""严格注册：任何系统不认识的契约级/字段级配置项都按非法契约拒绝。

包括顶层键拼写错误、字段约束名拼错、漂移配置里的未知指标名；
错误信息必须定位到具体位置（哪个契约 / 哪个字段 / 哪个键）。
"""

from __future__ import annotations

import pytest

from app.quality.contracts import normalize_contract
from app.quality.errors import ContractInvalidError, ErrorCategory


def reject(raw):
    with pytest.raises(Exception) as info:
        normalize_contract(raw)
    return info.value


def test_reject_unknown_top_level_key(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "freshnes_field": "ts",  # 顶层键拼错（少了 s）
    }
    case.input("raw", raw)
    case.expect("contract_invalid / unknown_contract_key，定位到顶层键")
    err = reject(raw)
    case.basis((err.reason, err.field, err.message))
    assert isinstance(err, ContractInvalidError)
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "unknown_contract_key"
    assert err.field == "freshnes_field"
    assert "顶层" in err.message


def test_reject_unknown_top_level_stray_option(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "threshold": 0.9,  # 系统不存在的契约级配置项
    }
    case.input("raw", raw)
    case.expect("unknown_contract_key: threshold")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "unknown_contract_key"
    assert err.field == "threshold"


def test_reject_misspelled_field_constraint(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer", "uniqe": True}],  # unique 拼错
    }
    case.input("raw", raw)
    case.expect("unknown_field_key，定位到字段 id 的 uniqe")
    err = reject(raw)
    case.basis((err.reason, err.field, err.message))
    assert err.category == ErrorCategory.CONTRACT_INVALID
    assert err.reason == "unknown_field_key"
    assert err.field == "uniqe"
    assert "id" in err.message and "fields[0]" in err.message


def test_reject_misspelled_nullable(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer", "nulable": True}],
    }
    case.input("raw", raw)
    case.expect("unknown_field_key: nulable（不能被当作默认 false 放过）")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "unknown_field_key"
    # 严格性证明：拼错的配置绝不能被静默当成默认值。
    spec = raw["fields"][0]
    assert "nullable" not in spec


def test_reject_unknown_field_key_identifies_field_index(case):
    raw = {
        "name": "c",
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "ts", "type": "timestamp", "tz": "UTC"},  # 第二个字段出错
        ],
    }
    case.input("raw", raw)
    case.expect("错误位置定位到 fields[1] / ts")
    err = reject(raw)
    case.basis((err.field, err.message))
    assert err.field == "tz"
    assert "fields[1]" in err.message and "ts" in err.message


def test_reject_unknown_drift_metric_name(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "drift_tolerance": {"compleetness": 0.1},  # 指标名拼错
    }
    case.input("raw", raw)
    case.expect("unknown_drift_metric")
    err = reject(raw)
    case.basis((err.reason, err.field))
    assert err.reason == "unknown_drift_metric"
    assert err.field == "compleetness"


def test_reject_drift_tolerance_wrong_shape(case):
    raw = {
        "name": "c",
        "fields": [{"name": "id", "type": "integer"}],
        "drift_tolerance": [0.1, 0.2],
    }
    case.input("drift_tolerance", raw["drift_tolerance"])
    case.expect("drift_tolerance_not_object")
    err = reject(raw)
    case.basis(err.reason)
    assert err.reason == "drift_tolerance_not_object"


def test_known_keys_still_register(case):
    """白名单内的全部已知配置项必须正常注册，证明严格模式没误伤。"""
    raw = {
        "name": "c",
        "version": 3,
        "fields": [
            {
                "name": "s",
                "type": "string",
                "required": False,
                "nullable": True,
                "min_length": 0,
                "max_length": 10,
                "regex": "^[a-z]*$",
                "allowed": ["a", "b"],
                "unique": True,
            }
        ],
        "freshness_field": "s",
        "freshness_max_age_seconds": 60,
        "drift_tolerance": {"completeness": 0.0, "uniqueness": 0.1,
                            "freshness": 0.2},
    }
    # freshness_field 必须是时间类型，换成独立契约避免语义冲突。
    raw = {
        "name": "c",
        "version": 3,
        "fields": [
            {
                "name": "s",
                "type": "string",
                "required": False,
                "nullable": True,
                "min_length": 0,
                "max_length": 10,
                "regex": "^[a-z]*$",
                "allowed": ["a", "b"],
                "unique": True,
            },
            {"name": "ts", "type": "timestamp"},
        ],
        "freshness_field": "ts",
        "freshness_max_age_seconds": 60,
        "drift_tolerance": {"completeness": 0.0, "uniqueness": 0.1,
                            "freshness": 0.2},
    }
    case.input("raw keys", sorted(raw.keys()))
    case.expect("全部已知键被接受")
    contract = normalize_contract(raw)
    case.basis((contract.version, contract.drift_tolerance))
    assert contract.version == 3
    assert contract.drift_tolerance == {
        "completeness": 0.0, "uniqueness": 0.1, "freshness": 0.2
    }
