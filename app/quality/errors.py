"""统一错误分类。

校验结论必须可区分三类问题：

1. 契约侧问题（CONTRACT_INVALID / CONSTRAINT_CONFLICT / FIELD_NOT_DECLARED）——
   契约本身非法或自相矛盾，属于"定义"错误；
2. 数据侧问题（DATA_VIOLATION / DATA_MISSING_FIELD）——
   契约合法，但被校验数据不满足约束，属于"数据"错误；
3. 依赖侧问题（DEPENDENCY_FAILURE）——
   底层校验依赖（类型系统、存储等）自身抛错，必须归一化后透出，
   不允许把依赖的原始异常直接抛给调用方。

此外 METRIC_NOT_FOUND 用于监控回溯时指标不存在。
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """可区分的错误类别。"""

    CONTRACT_INVALID = "contract_invalid"
    """契约本身结构非法（未知类型、空字段、min>max 等）。"""

    CONSTRAINT_CONFLICT = "constraint_conflict"
    """字段内或字段间约束相互冲突，无法同时成立。"""

    FIELD_NOT_DECLARED = "field_not_declared"
    """契约引用了一个未声明的字段。"""

    DATA_VIOLATION = "data_violation"
    """数据不满足契约约束（类型不符、取值越界等）。"""

    DATA_MISSING_FIELD = "data_missing_field"
    """数据行缺失契约要求的必填字段/值为 null。"""

    DEPENDENCY_FAILURE = "dependency_failure"
    """底层校验依赖自身故障，非数据问题、非契约问题。"""

    METRIC_NOT_FOUND = "metric_not_found"
    """请求回溯的指标批次/契约不存在。"""


class QualityError(Exception):
    """所有质量校验错误的统一基类。

    原始依赖异常通过 ``cause`` 保留，便于排查，但对调用方只暴露
    归一化后的类别与消息。
    """

    category: ErrorCategory = ErrorCategory.DEPENDENCY_FAILURE

    def __init__(
        self,
        message: str,
        *,
        contract: str | None = None,
        field: str | None = None,
        batch: str | None = None,
        reason: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.contract = contract
        self.field = field
        self.batch = batch
        # 机器可读的判定依据，用于日志与回溯。
        self.reason = reason or self.category.value
        self.cause = cause

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "reason": self.reason,
            "contract": self.contract,
            "field": self.field,
            "batch": self.batch,
        }


class ContractInvalidError(QualityError):
    category = ErrorCategory.CONTRACT_INVALID


class ConstraintConflictError(QualityError):
    category = ErrorCategory.CONSTRAINT_CONFLICT


class FieldNotDeclaredError(QualityError):
    category = ErrorCategory.FIELD_NOT_DECLARED


class DataViolationError(QualityError):
    category = ErrorCategory.DATA_VIOLATION


class DataMissingFieldError(QualityError):
    category = ErrorCategory.DATA_MISSING_FIELD


class DependencyFailureError(QualityError):
    category = ErrorCategory.DEPENDENCY_FAILURE


class MetricNotFoundError(QualityError):
    category = ErrorCategory.METRIC_NOT_FOUND
