"""数据契约模型：字段类型、必填、取值等约束归一为统一模型。

外部契约（dict / JSON 字符串）经 parse_contract 解析为不可变 Contract。
任何畸形定义、约束冲突、字段缺失都不会被静默跳过，而是抛出带
RejectReason 的 ContractRejectedError，检测顺序固定为：
MISSING_FIELD -> MALFORMED_CONTRACT -> CONFLICTING_CONSTRAINTS，
同一轮发现的多个问题聚合在 details 中一次性给出。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from dqv.errors import ContractRejectedError, RejectReason

_TOP_LEVEL_KEYS = {"name", "version", "fields", "freshness_max_age_seconds"}
_CONSTRAINT_KEYS = {
    "required",
    "nullable",
    "min_value",
    "max_value",
    "min_length",
    "max_length",
    "pattern",
    "allowed_values",
    "unique",
}
_NUMERIC_TYPES = {"integer", "number"}


class FieldType(str, Enum):
    """契约支持的归一化字段类型。"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


@dataclass(frozen=True)
class Constraints:
    """统一约束模型：必填/可空/取值范围/长度/正则/枚举/唯一。"""

    required: bool = True
    nullable: bool = False
    min_value: float | int | None = None
    min_value_open: bool = False
    max_value: float | int | None = None
    max_value_open: bool = False
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    allowed_values: tuple[object, ...] | None = None
    unique: bool = False


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    constraints: Constraints = field(default_factory=Constraints)


@dataclass(frozen=True)
class Contract:
    name: str
    version: int
    fields: tuple[FieldSpec, ...]
    freshness_max_age_seconds: float | None = None


def parse_contract(raw: object) -> Contract:
    """把外部输入（dict / JSON 字符串）解析为合法契约；非法即拒绝。

    检测轮次固定：
    1. 字段缺失（fields 为空、字段条目缺 name/type）；
    2. 畸形定义（JSON 非法、类型错误、未知键、坏正则等）；
    3. 约束冲突（min>max、枚举与范围/正则不交、字段重名）。
    """
    data = _coerce_mapping(raw)

    missing: list[str] = []
    malformed: list[str] = []
    raw_fields = data.get("fields", None)
    if "fields" not in data or raw_fields is None:
        missing.append("contract.fields: 字段定义缺失（fields 必须存在）")
    elif not isinstance(raw_fields, list) or len(raw_fields) == 0:
        malformed.append("contract.fields: 必须是非空列表")
    else:
        for index, raw_field in enumerate(raw_fields):
            if not isinstance(raw_field, Mapping):
                malformed.append(f"fields[{index}]: 必须是对象")
                continue
            if "name" not in raw_field:
                missing.append(f"fields[{index}]: 缺少字段名 name")
            if "type" not in raw_field:
                missing.append(f"fields[{index}].{raw_field.get('name', '?')}: 缺少类型 type")

    if missing:
        raise ContractRejectedError(RejectReason.MISSING_FIELD, "契约缺少必要字段定义", missing)
    if malformed:
        raise ContractRejectedError(RejectReason.MALFORMED_CONTRACT, "契约定义畸形", malformed)

    name = _parse_name(data, malformed)
    version = _parse_version(data, malformed)
    freshness = _parse_freshness(data, malformed)

    specs: list[FieldSpec] = []
    for index, raw_field in enumerate(raw_fields):  # type: ignore[assignment]
        specs.append(_parse_field(raw_field, index, malformed))  # type: ignore[arg-type]

    if malformed:
        raise ContractRejectedError(RejectReason.MALFORMED_CONTRACT, "契约定义畸形", malformed)

    conflicts = _check_conflicts(specs)
    if conflicts:
        raise ContractRejectedError(
            RejectReason.CONFLICTING_CONSTRAINTS, "契约约束相互冲突", conflicts
        )

    return Contract(
        name=name,
        version=version,
        fields=tuple(specs),
        freshness_max_age_seconds=freshness,
    )


def _coerce_mapping(raw: object) -> Mapping[str, object]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ContractRejectedError(
                RejectReason.MALFORMED_CONTRACT,
                f"契约 JSON 无法解析: {exc.__class__.__name__}",
                [f"JSON 解析失败: {exc}"],
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ContractRejectedError(
                RejectReason.MALFORMED_CONTRACT,
                "契约 JSON 顶层必须是对象",
                [f"实际类型: {type(decoded).__name__}"],
            )
        return decoded
    raise ContractRejectedError(
        RejectReason.MALFORMED_CONTRACT,
        "契约输入必须是对象或 JSON 字符串",
        [f"实际类型: {type(raw).__name__}"],
    )


def _parse_name(data: Mapping[str, object], malformed: list[str]) -> str:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        malformed.append("contract.name: 必须是非空字符串")
        return ""
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            malformed.append(f"contract: 未知顶层键 '{key}'")
    return name.strip()


def _parse_version(data: Mapping[str, object], malformed: list[str]) -> int:
    if "version" not in data:
        return 1
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        malformed.append("contract.version: 必须是 >=1 的整数")
        return 1
    return version


def _parse_freshness(data: Mapping[str, object], malformed: list[str]) -> float | None:
    if "freshness_max_age_seconds" not in data:
        return None
    value = data["freshness_max_age_seconds"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        malformed.append("contract.freshness_max_age_seconds: 必须是正数")
        return None
    return float(value)


def _parse_field(raw_field: Mapping[str, object], index: int, malformed: list[str]) -> FieldSpec:
    label = f"fields[{index}].{raw_field.get('name', '?')}"
    name = raw_field["name"]
    if not isinstance(name, str) or not name.strip():
        malformed.append(f"{label}: name 必须是非空字符串")
        name = str(name)

    type_value = raw_field["type"]
    try:
        field_type = FieldType(type_value)
    except ValueError:
        malformed.append(f"{label}: 未知字段类型 '{type_value}'")
        field_type = FieldType.STRING

    raw_constraints = raw_field.get("constraints", {})
    if not isinstance(raw_constraints, Mapping):
        malformed.append(f"{label}.constraints: 必须是对象")
        return FieldSpec(name=name, type=field_type)

    extra = set(raw_constraints) - _CONSTRAINT_KEYS
    for key in sorted(extra):
        malformed.append(f"{label}.constraints: 未知约束键 '{key}'")
    extra_field = set(raw_field) - {"name", "type", "constraints"}
    for key in sorted(extra_field):
        malformed.append(f"{label}: 未知字段键 '{key}'")

    constraints = _parse_constraints(raw_constraints, field_type, name, malformed)
    return FieldSpec(name=name, type=field_type, constraints=constraints)


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _parse_constraints(
    raw: Mapping[str, object],
    field_type: FieldType,
    field_name: str,
    malformed: list[str],
) -> Constraints:
    label = f"字段 '{field_name}'"

    required, nullable, unique = True, False, False
    if (b := _parse_bool(raw.get("required", True))) is None:
        malformed.append(f"{label}.constraints.required: 必须是布尔值")
    else:
        required = b
    if (b := _parse_bool(raw.get("nullable", False))) is None:
        malformed.append(f"{label}.constraints.nullable: 必须是布尔值")
    else:
        nullable = b
    if (b := _parse_bool(raw.get("unique", False))) is None:
        malformed.append(f"{label}.constraints.unique: 必须是布尔值")
    else:
        unique = b

    min_value = _parse_bound_number(raw.get("min_value"), "min_value", label, malformed)
    max_value = _parse_bound_number(raw.get("max_value"), "max_value", label, malformed)
    min_length = _parse_bound_int(raw.get("min_length"), "min_length", label, malformed)
    max_length = _parse_bound_int(raw.get("max_length"), "max_length", label, malformed)

    if field_type.value not in _NUMERIC_TYPES and (
        "min_value" in raw or "max_value" in raw
    ):
        malformed.append(f"{label}: 取值范围约束仅适用于数值类型（实际 {field_type.value}）")
    if field_type is not FieldType.STRING and (
        "min_length" in raw or "max_length" in raw or "pattern" in raw
    ):
        malformed.append(f"{label}: 长度/正则约束仅适用于 string 类型（实际 {field_type.value}）")

    pattern = raw.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            malformed.append(f"{label}.constraints.pattern: 必须是字符串")
            pattern = None
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                malformed.append(f"{label}.constraints.pattern: 非法正则 ({exc})")
                pattern = None

    allowed_values = _parse_allowed_values(raw.get("allowed_values"), field_type, label, malformed)

    return Constraints(
        required=required,
        nullable=nullable,
        min_value=min_value,
        max_value=max_value,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        allowed_values=allowed_values,
        unique=unique,
    )


def _parse_bound_number(
    value: object, key: str, label: str, malformed: list[str]
) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        malformed.append(f"{label}.constraints.{key}: 必须是数值")
        return None
    return value


def _parse_bound_int(
    value: object, key: str, label: str, malformed: list[str]
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        malformed.append(f"{label}.constraints.{key}: 必须是非负整数")
        return None
    return value


def _parse_allowed_values(
    value: object, field_type: FieldType, label: str, malformed: list[str]
) -> tuple[object, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) == 0:
        malformed.append(f"{label}.constraints.allowed_values: 必须是非空列表")
        return None
    normalized: list[object] = []
    for item in value:
        coerced = _coerce_literal(item, field_type)
        if coerced is _INVALID:
            malformed.append(
                f"{label}.constraints.allowed_values: 值 {item!r} 与类型 {field_type.value} 不兼容"
            )
            continue
        normalized.append(coerced)
    return tuple(normalized)


_INVALID = object()


def _coerce_literal(item: object, field_type: FieldType) -> object:
    """校验枚举字面量是否与声明类型兼容（不做隐式转换，bool 严格区分）。"""
    if field_type is FieldType.INTEGER:
        return item if isinstance(item, int) and not isinstance(item, bool) else _INVALID
    if field_type is FieldType.NUMBER:
        return item if isinstance(item, (int, float)) and not isinstance(item, bool) else _INVALID
    if field_type is FieldType.BOOLEAN:
        return item if isinstance(item, bool) else _INVALID
    if field_type is FieldType.STRING:
        return item if isinstance(item, str) else _INVALID
    if field_type is FieldType.DATE:
        if isinstance(item, str):
            try:
                datetime.strptime(item, "%Y-%m-%d")
                return item
            except ValueError:
                return _INVALID
        return _INVALID
    if field_type is FieldType.TIMESTAMP:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return item
        if isinstance(item, str):
            try:
                datetime.fromisoformat(item)
                return item
            except ValueError:
                return _INVALID
        return _INVALID
    return _INVALID


def _check_conflicts(specs: list[FieldSpec]) -> list[str]:
    conflicts: list[str] = []
    seen: dict[str, str] = {}
    for spec in specs:
        label = f"字段 '{spec.name}'"
        if spec.name in seen:
            conflicts.append(f"{label}: 字段名重复定义")
        seen[spec.name] = spec.type.value

        c = spec.constraints
        if c.min_value is not None and c.max_value is not None:
            if c.min_value > c.max_value:
                conflicts.append(
                    f"{label}: min_value({c.min_value}) > max_value({c.max_value})，区间为空"
                )
        if c.min_length is not None and c.max_length is not None:
            if c.min_length > c.max_length:
                conflicts.append(
                    f"{label}: min_length({c.min_length}) > max_length({c.max_length})，区间为空"
                )
        if c.allowed_values is not None:
            if spec.type.value in _NUMERIC_TYPES and (
                c.min_value is not None or c.max_value is not None
            ):
                low = c.min_value if c.min_value is not None else float("-inf")
                high = c.max_value if c.max_value is not None else float("inf")
                if not all(low <= v <= high for v in c.allowed_values):
                    conflicts.append(
                        f"{label}: 枚举集合 {list(c.allowed_values)} 与取值区间"
                        f"[{c.min_value}, {c.max_value}] 无交集"
                    )
            if spec.type is FieldType.STRING and c.pattern is not None:
                if not any(
                    isinstance(v, str) and re.fullmatch(c.pattern, v) for v in c.allowed_values
                ):
                    conflicts.append(
                        f"{label}: 枚举集合 {list(c.allowed_values)} 中无值匹配正则 {c.pattern!r}"
                    )
    return conflicts
