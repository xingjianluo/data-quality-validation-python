"""错误分类法：契约拒绝、数据违约、依赖故障三类互斥错误。

三类错误的语义：

* CONTRACT_REJECTED —— 契约本身不可受理（畸形 / 约束冲突 / 字段定义缺失），
  在进入数据校验前即被拒绝，必须给出可区分的 RejectReason。
* DATA_VIOLATION —— 契约合法、依赖正常，但数据不满足契约期望，
  携带逐项可解释的 Violation 列表。
* DEPENDENCY_FAILURE —— 底层校验依赖自身发生故障（如类型检查器异常、
  时间源不可用），只对外透出归一化后的错误，绝不直接透出原始异常。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum


class ErrorCategory(str, Enum):
    """顶层错误类别（互斥，可区分）。"""

    CONTRACT_REJECTED = "contract_rejected"
    DATA_VIOLATION = "data_violation"
    DEPENDENCY_FAILURE = "dependency_failure"


class RejectReason(str, Enum):
    """契约被拒绝的具体原因（可区分的拒绝原因）。"""

    MALFORMED_CONTRACT = "malformed_contract"
    CONFLICTING_CONSTRAINTS = "conflicting_constraints"
    MISSING_FIELD = "missing_field"


class ViolationCode(str, Enum):
    """数据不满足契约期望时的逐项违约码。"""

    FIELD_MISSING = "field_missing"
    REQUIRED_MISSING = "required_missing"
    NULL_NOT_ALLOWED = "null_not_allowed"
    TYPE_MISMATCH = "type_mismatch"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    LENGTH_OUT_OF_RANGE = "length_out_of_range"
    REGEX_MISMATCH = "regex_mismatch"
    NOT_IN_ALLOWED_VALUES = "not_in_allowed_values"
    DUPLICATE_KEY = "duplicate_key"


class DQVError(Exception):
    """所有数据质量错误的基类；每类错误归属唯一的 ErrorCategory。"""

    category: ErrorCategory

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, object]:
        return {"category": self.category.value, "message": self.message}


class ContractRejectedError(DQVError):
    """契约本身非法 / 约束冲突 / 字段缺失，拒绝受理。"""

    category = ErrorCategory.CONTRACT_REJECTED

    def __init__(
        self,
        reason: RejectReason,
        message: str,
        details: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details: list[str] = list(details or [])

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload["reason"] = self.reason.value
        payload["details"] = self.details
        return payload


class DataValidationError(DQVError):
    """数据不满足契约期望（与依赖自身故障严格区分）。

    典型用法：调用方选择“以异常方式”获得违约结论时抛出；
    validate_records 默认返回 ValidationResult，不抛此异常。
    """

    category = ErrorCategory.DATA_VIOLATION

    def __init__(self, message: str, violations: Sequence[object] | None = None) -> None:
        super().__init__(message)
        self.violations: list[object] = list(violations or [])

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload["violation_count"] = len(self.violations)
        return payload


class DependencyFailureError(DQVError):
    """底层校验依赖自身故障；归一化后对外透出，不泄露内部异常细节。"""

    category = ErrorCategory.DEPENDENCY_FAILURE

    def __init__(self, message: str, component: str = "unknown") -> None:
        super().__init__(message)
        self.component = component

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload["component"] = self.component
        return payload
