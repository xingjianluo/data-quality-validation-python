"""数据契约的统一模型与归一化。

字段类型、必填、取值范围等约束都归一为 :class:`FieldSpec`；
非法契约 / 冲突约束 / 字段缺失在归一化阶段以可区分错误拒绝，
绝不静默跳过，也不返回"部分可用"的契约。

拒绝原因（reason）对照：

* ``contract_*`` / ``field_*``  -> :class:`ContractInvalidError`
* ``duplicate_field`` / ``*_out_of_range`` 等 -> :class:`ConstraintConflictError`
* ``freshness_field_undeclared`` -> :class:`FieldNotDeclaredError`
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.quality.errors import (
    ConstraintConflictError,
    ContractInvalidError,
    FieldNotDeclaredError,
)


class DataType(str, Enum):
    """归一化后的字段类型。"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


# 各类型允许的 Python 原生值类型（bool 必须先于 int 判断）。
_PY_TYPES: dict[DataType, tuple[type, ...]] = {
    DataType.STRING: (str,),
    DataType.INTEGER: (int,),
    DataType.NUMBER: (int, float),
    DataType.BOOLEAN: (bool,),
    DataType.DATE: (str, date, datetime),
    DataType.TIMESTAMP: (str, date, datetime),
}

_TEMPORAL = (DataType.DATE, DataType.TIMESTAMP)
_NUMERIC = (DataType.INTEGER, DataType.NUMBER)

# 契约级 / 字段级允许出现的配置项白名单。注册严格模式：出现白名单外的
# 键（含拼写错误，如 frehsness_field / uniqe）一律按契约非法拒绝。
_CONTRACT_KEYS = frozenset(
    {
        "name",
        "version",
        "fields",
        "freshness_field",
        "freshness_max_age_seconds",
        "drift_tolerance",
    }
)
_FIELD_KEYS = frozenset(
    {
        "name",
        "type",
        "required",
        "nullable",
        "min_value",
        "max_value",
        "allowed",
        "min_length",
        "max_length",
        "unique",
        "regex",
    }
)
# 漂移容忍幅度可配置的指标名。
_DRIFT_METRIC_KEYS = frozenset({"completeness", "uniqueness", "freshness"})


class FieldSpec(BaseModel):
    """单字段的统一约束模型。"""

    model_config = {"extra": "forbid"}

    name: str
    type: DataType
    required: bool = True
    nullable: bool = False
    min_value: float | int | None = None
    max_value: float | int | None = None
    allowed: list[Any] | None = None
    min_length: int | None = None
    max_length: int | None = None
    unique: bool = False
    regex: str | None = None


class Contract(BaseModel):
    """一份数据契约。"""

    model_config = {"populate_by_name": True, "extra": "forbid"}

    name: str
    version: int = 1
    fields_: list[FieldSpec] = Field(default_factory=list, alias="fields")
    # 时间字段及其 SLA，供刷新时效指标使用。
    freshness_field: str | None = None
    freshness_max_age_seconds: float | None = None
    # 每个指标相对基线允许的最大下滑幅度（[0,1]），在批次首次判定时固化。
    drift_tolerance: dict[str, float] = Field(default_factory=dict)

    @property
    def fields(self) -> dict[str, FieldSpec]:
        return {spec.name: spec for spec in self.fields_}

    def get(self, name: str) -> FieldSpec:
        spec = self.fields.get(name)
        if spec is None:
            raise FieldNotDeclaredError(
                f"契约 {self.name} 未声明字段 {name}",
                contract=self.name,
                field=name,
                reason="field_undeclared",
            )
        return spec


def normalize_contract(raw: dict[str, Any]) -> Contract:
    """把原始 dict 契约归一化为合法 :class:`Contract`。

    非法 / 冲突 / 字段缺失时抛出可区分错误，不返回部分结果；
    底层 pydantic 解析异常被归一化，不直接透出。
    """
    if not isinstance(raw, dict):
        raise ContractInvalidError(
            "契约必须是 JSON 对象", reason="contract_not_object"
        )

    unknown_contract_keys = sorted(set(raw) - _CONTRACT_KEYS)
    if unknown_contract_keys:
        # 严格注册：不认识的契约级配置项（含拼写错误）必须显式拒绝，
        # 不能被当成"没写"悄悄放过。
        raise ContractInvalidError(
            f"契约 {raw.get('name')!r} 含不认识的契约级配置项 "
            f"{unknown_contract_keys[0]}（位置：契约顶层）",
            contract=raw.get("name") if isinstance(raw.get("name"), str) else None,
            field=unknown_contract_keys[0],
            reason="unknown_contract_key",
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ContractInvalidError(
            "契约 name 不能为空字符串", reason="contract_name_blank"
        )

    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ContractInvalidError(
            f"契约 {name} 的 version 必须是 >=1 的整数，实际为 {version!r}",
            contract=name,
            reason="contract_version_invalid",
        )

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or len(raw_fields) == 0:
        raise ContractInvalidError(
            f"契约 {name} 必须至少声明一个字段",
            contract=name,
            reason="fields_empty",
        )

    specs: list[FieldSpec] = []
    seen: set[str] = set()
    for index, rf in enumerate(raw_fields):
        if not isinstance(rf, dict):
            raise ContractInvalidError(
                f"契约 {name} 的第 {index} 个字段定义必须是对象",
                contract=name,
                reason="field_not_object",
            )
        fname = rf.get("name")
        if not isinstance(fname, str) or not fname.strip():
            raise ContractInvalidError(
                f"契约 {name} 的第 {index} 个字段缺少有效 name",
                contract=name,
                reason="field_name_blank",
            )
        unknown_field_keys = sorted(set(rf) - _FIELD_KEYS)
        if unknown_field_keys:
            # 严格注册：字段级不认识的配置项（如 uniqe / nulable）显式拒绝，
            # field 记录不认识的键名，消息定位到"哪个契约的哪个字段(索引)"。
            bad_key = unknown_field_keys[0]
            raise ContractInvalidError(
                f"契约 {name} 的字段 {fname} 含不认识的字段级配置项 "
                f"{bad_key}（位置：fields[{index}].{bad_key}）",
                contract=name,
                field=bad_key,
                reason="unknown_field_key",
            )
        if fname in seen:
            # 同名重复 = 对同一字段给出了两套互相冲突的定义。
            raise ConstraintConflictError(
                f"字段 {fname} 在契约 {name} 中重复声明",
                contract=name,
                field=fname,
                reason="duplicate_field",
            )
        seen.add(fname)

        try:
            spec = FieldSpec(**rf)
        except ValidationError as exc:
            # 依赖（pydantic）的解析错误归一化为"契约非法"，不透出原始异常。
            detail = exc.errors()[0]
            raise ContractInvalidError(
                f"字段 {fname} 定义非法: {'.'.join(map(str, detail['loc']))} "
                f"{detail['msg']}",
                contract=name,
                field=fname,
                reason="field_definition_invalid",
                cause=exc,
            ) from exc

        _assert_valid(name, spec)
        _assert_no_conflict(name, spec)
        specs.append(spec)

    freshness_field = raw.get("freshness_field")
    freshness_max = raw.get("freshness_max_age_seconds")

    if freshness_field is not None:
        if not isinstance(freshness_field, str) or freshness_field not in seen:
            raise FieldNotDeclaredError(
                f"freshness_field 引用了契约 {name} 中未声明的字段 "
                f"{freshness_field!r}",
                contract=name,
                field=freshness_field if isinstance(freshness_field, str) else None,
                reason="freshness_field_undeclared",
            )
        target = next(s for s in specs if s.name == freshness_field)
        if target.type not in _TEMPORAL:
            raise ContractInvalidError(
                f"freshness_field {freshness_field} 必须是 date/timestamp 类型，"
                f"实际为 {target.type.value}",
                contract=name,
                field=freshness_field,
                reason="freshness_field_wrong_type",
            )

    if freshness_max is not None:
        if isinstance(freshness_max, bool) or not isinstance(
            freshness_max, (int, float)
        ):
            raise ContractInvalidError(
                "freshness_max_age_seconds 必须是非负数值",
                contract=name,
                reason="freshness_max_age_invalid",
            )
        if freshness_max < 0:
            raise ContractInvalidError(
                "freshness_max_age_seconds 不能为负数",
                contract=name,
                reason="freshness_max_age_negative",
            )
        if freshness_field is None:
            raise ContractInvalidError(
                "设置了 freshness_max_age_seconds 但未指定 freshness_field",
                contract=name,
                reason="freshness_field_missing",
            )

    raw_tolerance = raw.get("drift_tolerance")
    drift_tolerance: dict[str, float] = {}
    if raw_tolerance is not None:
        if not isinstance(raw_tolerance, dict):
            raise ContractInvalidError(
                "drift_tolerance 必须是以指标名为键的对象",
                contract=name,
                reason="drift_tolerance_not_object",
            )
        for metric_name, tol in raw_tolerance.items():
            if metric_name not in _DRIFT_METRIC_KEYS:
                raise ContractInvalidError(
                    f"drift_tolerance 含不认识的指标名 {metric_name!r}，"
                    f"允许值为 {sorted(_DRIFT_METRIC_KEYS)}",
                    contract=name,
                    field=metric_name,
                    reason="unknown_drift_metric",
                )
            if isinstance(tol, bool) or not isinstance(tol, (int, float)):
                raise ContractInvalidError(
                    f"drift_tolerance.{metric_name} 必须是数值",
                    contract=name,
                    field=metric_name,
                    reason="drift_tolerance_not_number",
                )
            if tol < 0 or tol > 1:
                raise ContractInvalidError(
                    f"drift_tolerance.{metric_name} 必须落在 [0, 1]，实际为 {tol}",
                    contract=name,
                    field=metric_name,
                    reason="drift_tolerance_out_of_range",
                )
            drift_tolerance[metric_name] = float(tol)

    try:
        return Contract(
            name=name,
            version=version,
            fields=specs,
            freshness_field=freshness_field,
            freshness_max_age_seconds=freshness_max,
            drift_tolerance=drift_tolerance,
        )
    except ValidationError as exc:  # pragma: no cover - 兜底，理论不可达
        raise ContractInvalidError(
            f"契约 {name} 组装失败: {exc}",
            contract=name,
            reason="contract_assemble_failed",
            cause=exc,
        ) from exc


def _assert_valid(contract: str, spec: FieldSpec) -> None:
    """结构性 / 类型语义非法 -> CONTRACT_INVALID。"""

    if spec.min_length is not None and spec.min_length < 0:
        raise ContractInvalidError(
            f"字段 {spec.name} 的 min_length 不能为负",
            contract=contract,
            field=spec.name,
            reason="min_length_negative",
        )
    if spec.max_length is not None and spec.max_length < 0:
        raise ContractInvalidError(
            f"字段 {spec.name} 的 max_length 不能为负",
            contract=contract,
            field=spec.name,
            reason="max_length_negative",
        )
    if (spec.min_length is not None or spec.max_length is not None) and (
        spec.type is not DataType.STRING
    ):
        raise ContractInvalidError(
            f"长度约束只能用于 string 字段，{spec.name} 是 {spec.type.value}",
            contract=contract,
            field=spec.name,
            reason="length_on_non_string",
        )
    if (spec.min_value is not None or spec.max_value is not None) and (
        spec.type not in _NUMERIC
    ):
        raise ContractInvalidError(
            f"取值上下界只能用于 integer/number 字段，{spec.name} 是 "
            f"{spec.type.value}",
            contract=contract,
            field=spec.name,
            reason="range_on_non_numeric",
        )
    if spec.regex is not None and spec.type is not DataType.STRING:
        raise ContractInvalidError(
            f"正则约束只能用于 string 字段，{spec.name} 是 {spec.type.value}",
            contract=contract,
            field=spec.name,
            reason="regex_on_non_string",
        )
    if spec.allowed is not None:
        if not isinstance(spec.allowed, list):  # pragma: no cover - pydantic 保证
            raise ContractInvalidError(
                f"字段 {spec.name} 的 allowed 必须是列表",
                contract=contract,
                field=spec.name,
                reason="allowed_not_list",
            )
        for value in spec.allowed:
            if not _matches_type(spec.type, value):
                raise ContractInvalidError(
                    f"字段 {spec.name} 声明为 {spec.type.value}，"
                    f"但 allowed 含不兼容值 {value!r}",
                    contract=contract,
                    field=spec.name,
                    reason="allowed_type_mismatch",
                )


def _assert_no_conflict(contract: str, spec: FieldSpec) -> None:
    """约束间无法同时成立 -> CONSTRAINT_CONFLICT。"""

    if (
        spec.min_value is not None
        and spec.max_value is not None
        and spec.min_value > spec.max_value
    ):
        raise ConstraintConflictError(
            f"字段 {spec.name} 的 min_value {spec.min_value} > "
            f"max_value {spec.max_value}",
            contract=contract,
            field=spec.name,
            reason="min_gt_max_value",
        )
    if (
        spec.min_length is not None
        and spec.max_length is not None
        and spec.min_length > spec.max_length
    ):
        raise ConstraintConflictError(
            f"字段 {spec.name} 的 min_length {spec.min_length} > "
            f"max_length {spec.max_length}",
            contract=contract,
            field=spec.name,
            reason="min_gt_max_length",
        )
    if spec.allowed is not None and len(spec.allowed) == 0:
        # 空白名单否定了一切取值，包括必填字段本身。
        raise ConstraintConflictError(
            f"字段 {spec.name} 的 allowed 为空，任何取值都被拒绝",
            contract=contract,
            field=spec.name,
            reason="allowed_empty",
        )
    if spec.allowed is not None:
        for value in spec.allowed:
            if spec.type in _NUMERIC:
                if spec.min_value is not None and value < spec.min_value:
                    raise ConstraintConflictError(
                        f"字段 {spec.name} 的 allowed 值 {value!r} 小于 "
                        f"min_value {spec.min_value}",
                        contract=contract,
                        field=spec.name,
                        reason="allowed_below_min",
                    )
                if spec.max_value is not None and value > spec.max_value:
                    raise ConstraintConflictError(
                        f"字段 {spec.name} 的 allowed 值 {value!r} 大于 "
                        f"max_value {spec.max_value}",
                        contract=contract,
                        field=spec.name,
                        reason="allowed_above_max",
                    )
            if spec.type is DataType.STRING:
                if spec.min_length is not None and len(value) < spec.min_length:
                    raise ConstraintConflictError(
                        f"字段 {spec.name} 的 allowed 值 {value!r} 短于 "
                        f"min_length {spec.min_length}",
                        contract=contract,
                        field=spec.name,
                        reason="allowed_shorter_than_min",
                    )
                if spec.max_length is not None and len(value) > spec.max_length:
                    raise ConstraintConflictError(
                        f"字段 {spec.name} 的 allowed 值 {value!r} 长于 "
                        f"max_length {spec.max_length}",
                        contract=contract,
                        field=spec.name,
                        reason="allowed_longer_than_max",
                    )


def _matches_type(dtype: DataType, value: Any) -> bool:
    """判断字面量值是否与声明类型兼容（归一化阶段静态使用）。"""

    if dtype is DataType.INTEGER and isinstance(value, bool):
        return False
    if not isinstance(value, _PY_TYPES[dtype]):
        return False
    if dtype in _TEMPORAL and isinstance(value, str):
        return _parse_temporal(dtype, value) is not None
    return True


def _parse_temporal(dtype: DataType, value: str) -> datetime | date | None:
    """解析 ISO8601 日期/时间字符串，失败返回 None。"""

    try:
        if dtype is DataType.TIMESTAMP:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
