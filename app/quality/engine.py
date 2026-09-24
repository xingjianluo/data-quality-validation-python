"""校验执行引擎与异常归一化。

错误分类边界：

* 契约已合法，数据不满足约束 -> ``DATA_VIOLATION``；
* 数据行缺字段 / 必填值缺失 / null -> ``DATA_MISSING_FIELD``；
* 底层校验依赖（可注入的 :class:`TypeChecker`）自身抛异常 ->
  ``DEPENDENCY_FAILURE``，原始异常仅记录在 ``cause``，不直接透出。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.quality.contracts import (
    DataType,
    FieldSpec,
    Contract,
    _parse_temporal,
)
from app.quality.errors import DependencyFailureError

# bool 不是合法的整数/浮点取值，需要先排除。
def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _coerce_number(v: Any) -> float:
    if _is_bool(v):
        raise TypeError("bool 不是数值")
    if isinstance(v, (int, float)):
        return float(v)
    raise TypeError("非数值")


class DefaultTypeChecker:
    """默认的类型/取值校验依赖。

    返回 None 表示通过；返回规则名字符串表示数据违规。
    任何未预期异常都视为依赖自身故障，由引擎捕获归一化。
    """

    def check(self, spec: FieldSpec, value: Any) -> str | None:
        t = spec.type
        if t is DataType.STRING:
            if not isinstance(value, str):
                return "type_mismatch"
            if spec.min_length is not None and len(value) < spec.min_length:
                return "min_length"
            if spec.max_length is not None and len(value) > spec.max_length:
                return "max_length"
            if spec.regex is not None and re.fullmatch(spec.regex, value) is None:
                return "regex"
        elif t is DataType.INTEGER:
            if _is_bool(value) or not isinstance(value, int):
                return "type_mismatch"
            return self._check_numeric_bounds(spec, value)
        elif t is DataType.NUMBER:
            if _is_bool(value) or not isinstance(value, (int, float)):
                return "type_mismatch"
            return self._check_numeric_bounds(spec, value)
        elif t is DataType.BOOLEAN:
            if not isinstance(value, bool):
                return "type_mismatch"
        elif t is DataType.DATE:
            if isinstance(value, datetime) or isinstance(value, date):
                return None
            if not isinstance(value, str) or _parse_temporal(t, value) is None:
                return "type_mismatch"
        elif t is DataType.TIMESTAMP:
            if isinstance(value, datetime):
                return None
            if not isinstance(value, str) or _parse_temporal(t, value) is None:
                return "type_mismatch"

        if spec.allowed is not None and value not in spec.allowed:
            return "allowed"
        return None

    @staticmethod
    def _check_numeric_bounds(spec: FieldSpec, value: Any) -> str | None:
        if spec.min_value is not None and value < spec.min_value:
            return "min_value"
        if spec.max_value is not None and value > spec.max_value:
            return "max_value"
        return None


class Violation(BaseModel):
    """单条数据的单个约束违背。"""

    row: int
    field: str
    rule: str
    expected: str
    actual: Any
    category: str


class ValidationResult(BaseModel):
    """一次校验执行的结论（可重复执行、结论确定）。"""

    batch_id: str
    contract: str
    passed: bool
    total_rows: int
    violations: list[Violation]

    @property
    def violation_count(self) -> int:
        return len(self.violations)


class TypeChecker(Protocol):
    """底层类型/取值校验依赖的抽象，便于注入故障依赖做测试。"""

    def check(self, spec: FieldSpec, value: Any) -> str | None:
        ...


def _describe_expected(spec: FieldSpec, rule: str) -> str:
    if rule == "type_mismatch":
        return f"类型为 {spec.type.value}"
    if rule == "min_value":
        return f">= {spec.min_value}"
    if rule == "max_value":
        return f"<= {spec.max_value}"
    if rule == "min_length":
        return f"长度 >= {spec.min_length}"
    if rule == "max_length":
        return f"长度 <= {spec.max_length}"
    if rule == "regex":
        return f"匹配正则 {spec.regex}"
    if rule == "allowed":
        return f"取值属于 {spec.allowed!r}"
    if rule == "unique":
        return f"字段 {spec.name} 全局唯一"
    return rule


class ValidationEngine:
    """对一批数据执行契约校验（纯函数式，无副作用、结论确定）。"""

    def __init__(self, checker: TypeChecker | None = None) -> None:
        self._checker: TypeChecker = checker or DefaultTypeChecker()

    def validate(
        self, contract: Contract, rows: list[dict[str, Any]], batch_id: str
    ) -> ValidationResult:
        violations: list[Violation] = []

        # 唯一性：契约声明 unique 的字段，在整批范围内不得重复（null 不参与）。
        unique_seen: dict[str, set[Any]] = {
            spec.name: set() for spec in contract.fields_ if spec.unique
        }

        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise DependencyFailureError(
                    f"第 {idx} 行不是记录对象，读取器可能故障",
                    contract=contract.name,
                    batch=batch_id,
                    reason="row_not_object",
                )
            for spec in contract.fields.values():
                present = spec.name in row
                value = row.get(spec.name)

                if not present or value is None:
                    # 缺字段与 null 统一按"缺失"处理：
                    # - 可选字段缺列：允许；
                    # - 值为 null 或缺列但声明 nullable：允许；
                    # - 其余（必填缺失 / 可选但禁止 null）：缺失违规。
                    if not present and not spec.required:
                        continue
                    if spec.nullable:
                        continue
                    expected = "字段存在且非 null" if spec.required else "值非 null"
                    violations.append(
                        Violation(
                            row=idx,
                            field=spec.name,
                            rule="missing",
                            expected=expected,
                            actual=None,
                            category="data_missing_field",
                        )
                    )
                    continue

                v = self._check_row(idx, spec, value, batch_id, contract.name)
                if v is not None:
                    violations.append(v)
                    continue

                if spec.name in unique_seen:
                    if value in unique_seen[spec.name]:
                        violations.append(
                            Violation(
                                row=idx,
                                field=spec.name,
                                rule="unique",
                                expected=_describe_expected(spec, "unique"),
                                actual=value,
                                category="data_violation",
                            )
                        )
                    else:
                        unique_seen[spec.name].add(value)

        return ValidationResult(
            batch_id=batch_id,
            contract=contract.name,
            passed=not violations,
            total_rows=len(rows),
            violations=violations,
        )

    def _check_row(
        self,
        idx: int,
        spec: FieldSpec,
        value: Any,
        batch_id: str,
        contract_name: str,
    ) -> Violation | None:
        try:
            rule = self._checker.check(spec, value)
        except Exception as exc:  # 依赖自身故障：归一化，不透出原始异常
            raise DependencyFailureError(
                f"校验依赖在字段 {spec.name} 上发生内部故障，已中止该批次",
                contract=contract_name,
                field=spec.name,
                batch=batch_id,
                reason="checker_raised",
                cause=exc,
            ) from exc

        if rule is None:
            return None
        return Violation(
            row=idx,
            field=spec.name,
            rule=rule,
            expected=_describe_expected(spec, rule),
            actual=value,
            category="data_violation",
        )
