"""校验执行：把记录与归一化契约比对，输出可解释的违约项。

关键边界：
* 数据不满足期望 -> Violation（类别 DATA_VIOLATION），每条违约记录
  行号、字段、违约码、期望值、实际值与判定依据；
* 校验依赖（CheckerBackend）自身抛出的任何异常，都在此边界被捕获并
  归一化为 DependencyFailureError，原始异常不直接透出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from dqv.contracts import Contract, Constraints, FieldSpec, FieldType
from dqv.errors import (
    DependencyFailureError,
    DataValidationError,
    ViolationCode,
)


@dataclass(frozen=True)
class Violation:
    row_index: int
    field_name: str
    code: ViolationCode
    expected: str
    actual: str
    basis: str

    def to_dict(self) -> dict[str, object]:
        return {
            "row_index": self.row_index,
            "field_name": self.field_name,
            "code": self.code.value,
            "expected": self.expected,
            "actual": self.actual,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class ValidationResult:
    contract_name: str
    contract_version: int
    batch_id: str
    passed: bool
    rows_checked: int
    violations: tuple[Violation, ...]
    evaluated_at: str

    def signature(self) -> tuple[object, ...]:
        """除评估时间外的结论指纹；同输入重复执行时结论应一致。"""
        return (
            self.contract_name,
            self.contract_version,
            self.batch_id,
            self.passed,
            self.rows_checked,
            tuple(sorted(v.to_dict().items() for v in self.violations)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
            "batch_id": self.batch_id,
            "passed": self.passed,
            "rows_checked": self.rows_checked,
            "violations": [v.to_dict() for v in self.violations],
            "evaluated_at": self.evaluated_at,
        }


class CheckerBackend(Protocol):
    """底层校验依赖协议；其自身故障需被归一化，不得直接透出。"""

    component: str

    def check(self, spec: FieldSpec, value: object) -> tuple[bool, str]:
        """对非空单元格做类型与取值检查；返回 (是否通过, 失败说明)。"""
        ...


class StdlibCheckerBackend:
    """仅依赖标准库的默认检查后端。"""

    component = "stdlib_checker"

    def check(self, spec: FieldSpec, value: object) -> tuple[bool, str]:
        coerced = _check_type(spec.type, value)
        if coerced is _TYPE_FAILED:
            return False, f"值 {value!r} 不是声明类型 {spec.type.value}"
        return _check_constraints(spec, value)


def default_backend() -> CheckerBackend:
    return StdlibCheckerBackend()


def validate_records(
    contract: Contract,
    records: list[dict[str, Any]],
    batch_id: str,
    *,
    backend: CheckerBackend | None = None,
) -> ValidationResult:
    """对数据集执行契约校验；依赖故障归一化为 DependencyFailureError。"""
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise DataValidationError("records 必须是 dict 记录的列表")

    checker = backend or default_backend()
    violations: list[Violation] = []

    try:
        for row_index, record in enumerate(records):
            violations.extend(_check_row(contract, record, row_index, checker))
        violations.extend(_check_uniqueness(contract, records))
    except DependencyFailureError:
        raise
    except DataValidationError:
        raise
    except Exception as exc:  # 防御式归一化：依赖内部异常绝不直接透出
        raise DependencyFailureError(
            f"校验依赖在执行中发生内部故障（细节已归一化）",
            component=getattr(checker, "component", "unknown"),
        ) from exc

    return ValidationResult(
        contract_name=contract.name,
        contract_version=contract.version,
        batch_id=batch_id,
        passed=not violations,
        rows_checked=len(records),
        violations=tuple(violations),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )


def _check_row(
    contract: Contract, record: dict[str, Any], row_index: int, checker: CheckerBackend
) -> list[Violation]:
    found: list[Violation] = []
    for spec in contract.fields:
        present = spec.name in record
        value = record.get(spec.name)
        basis = f"字段 '{spec.name}' 声明类型 {spec.type.value}，约束 {_describe(spec.constraints)}"

        if not present:
            if spec.constraints.required:
                found.append(
                    Violation(
                        row_index, spec.name, ViolationCode.REQUIRED_MISSING,
                        expected="必填字段必须出现",
                        actual="<字段缺失>",
                        basis=basis,
                    )
                )
            continue

        if value is None:
            if not spec.constraints.nullable:
                found.append(
                    Violation(
                        row_index, spec.name, ViolationCode.NULL_NOT_ALLOWED,
                        expected="不允许为 null",
                        actual="None",
                        basis=basis,
                    )
                )
            continue

        try:
            ok, reason = checker.check(spec, value)
        except Exception as exc:
            raise DependencyFailureError(
                "类型/取值检查依赖发生内部故障（细节已归一化）",
                component=getattr(checker, "component", "unknown"),
            ) from exc
        if not ok:
            found.append(_violation_from_reason(spec, row_index, value, reason, basis))
    return found


def _violation_from_reason(
    spec: FieldSpec,
    row_index: int,
    value: object,
    reason: str,
    basis: str,
) -> Violation:
    text = reason.lower()
    if "不是声明类型" in reason:
        code = ViolationCode.TYPE_MISMATCH
        expected = f"类型 {spec.type.value}"
    elif "取值区间" in reason:
        code = ViolationCode.VALUE_OUT_OF_RANGE
        expected = _range_expected(spec.constraints)
    elif "长度" in reason:
        code = ViolationCode.LENGTH_OUT_OF_RANGE
        expected = _length_expected(spec.constraints)
    elif "正则" in reason:
        code = ViolationCode.REGEX_MISMATCH
        expected = f"匹配正则 {spec.constraints.pattern!r}"
    elif "枚举" in reason:
        code = ViolationCode.NOT_IN_ALLOWED_VALUES
        expected = f"取值属于 {list(spec.constraints.allowed_values)}"
    else:  # pragma: no cover - 后端消息契约兜底
        code = ViolationCode.TYPE_MISMATCH
        expected = spec.type.value
    return Violation(row_index, spec.name, code, expected, repr(value), basis + f"；后端判定：{text}")


def _check_uniqueness(contract: Contract, records: list[dict[str, Any]]) -> list[Violation]:
    found: list[Violation] = []
    for spec in contract.fields:
        if not spec.constraints.unique:
            continue
        seen: dict[object, int] = {}
        basis = f"字段 '{spec.name}' 声明 unique=True，重复值除首次出现外均违约"
        for row_index, record in enumerate(records):
            if spec.name not in record:
                continue
            value = record[spec.name]
            if value is None:
                continue
            if value in seen:
                found.append(
                    Violation(
                        row_index, spec.name, ViolationCode.DUPLICATE_KEY,
                        expected=f"值在字段 '{spec.name}' 中唯一（首次出现于第 {seen[value]} 行）",
                        actual=repr(value),
                        basis=basis,
                    )
                )
            else:
                seen[value] = row_index
    return found


def _describe(c: Constraints) -> str:
    parts = [f"required={c.required}", f"nullable={c.nullable}"]
    if c.min_value is not None or c.max_value is not None:
        parts.append(f"range={_range_expected(c)}")
    if c.min_length is not None or c.max_length is not None:
        parts.append(f"length={_length_expected(c)}")
    if c.pattern is not None:
        parts.append(f"pattern={c.pattern!r}")
    if c.allowed_values is not None:
        parts.append(f"allowed={list(c.allowed_values)}")
    if c.unique:
        parts.append("unique=True")
    return "{" + ", ".join(parts) + "}"


def _range_expected(c: Constraints) -> str:
    return f"[{c.min_value if c.min_value is not None else '-inf'}, {c.max_value if c.max_value is not None else '+inf'}]"


def _length_expected(c: Constraints) -> str:
    return f"[{c.min_length if c.min_length is not None else 0}, {c.max_length if c.max_length is not None else '∞'}]"


_TYPE_FAILED = object()


def _check_type(field_type: FieldType, value: object) -> object:
    if field_type is FieldType.STRING:
        return value if isinstance(value, str) else _TYPE_FAILED
    if field_type is FieldType.INTEGER:
        return value if isinstance(value, int) and not isinstance(value, bool) else _TYPE_FAILED
    if field_type is FieldType.NUMBER:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else _TYPE_FAILED
    if field_type is FieldType.BOOLEAN:
        return value if isinstance(value, bool) else _TYPE_FAILED
    if field_type is FieldType.DATE:
        if isinstance(value, str):
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return value
            except ValueError:
                return _TYPE_FAILED
        return _TYPE_FAILED
    if field_type is FieldType.TIMESTAMP:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value)
                return value
            except ValueError:
                return _TYPE_FAILED
        return _TYPE_FAILED
    return _TYPE_FAILED  # pragma: no cover


def _check_constraints(spec: FieldSpec, value: object) -> tuple[bool, str]:
    c = spec.constraints
    if spec.type.value in ("integer", "number"):
        if c.min_value is not None and value < c.min_value:
            return False, f"值 {value!r} 低于取值区间下界 {c.min_value}"
        if c.max_value is not None and value > c.max_value:
            return False, f"值 {value!r} 高于取值区间上界 {c.max_value}"
    if spec.type is FieldType.STRING:
        length = len(value)  # type: ignore[arg-type]
        if c.min_length is not None and length < c.min_length:
            return False, f"字符串长度 {length} 短于最小长度 {c.min_length}"
        if c.max_length is not None and length > c.max_length:
            return False, f"字符串长度 {length} 超过最大长度 {c.max_length}"
        if c.pattern is not None and re.fullmatch(c.pattern, value) is None:  # type: ignore[arg-type]
            return False, f"值 {value!r} 不匹配正则 {c.pattern!r}"
    if c.allowed_values is not None and value not in c.allowed_values:
        return False, f"值 {value!r} 不在枚举集合 {list(c.allowed_values)} 中"
    return True, ""
