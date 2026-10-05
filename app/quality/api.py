"""HTTP 接口：注册契约、提交校验、回溯历史。

所有 :class:`QualityError` 统一转换为带 ``category`` 的 JSON，
HTTP 状态码与错误大类对应，调用方据此区分契约/数据/依赖问题。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.quality.errors import (
    ErrorCategory,
    FieldNotDeclaredError,
    MetricNotFoundError,
    QualityError,
)
from app.quality.service import QualityService

# 错误大类 -> HTTP 状态码。
_STATUS = {
    ErrorCategory.CONTRACT_INVALID: 422,
    ErrorCategory.CONSTRAINT_CONFLICT: 409,
    ErrorCategory.FIELD_NOT_DECLARED: 404,
    ErrorCategory.DATA_VIOLATION: 200,       # 校验成功执行，数据不合规仍返回结论
    ErrorCategory.DATA_MISSING_FIELD: 200,
    ErrorCategory.DEPENDENCY_FAILURE: 503,
    ErrorCategory.METRIC_NOT_FOUND: 404,
}


class SubmitRequest(BaseModel):
    rows: list[dict[str, Any]]
    batch_id: str
    # 可选：显式指定漂移对比的基线批次；缺省取最近一次已判定批次。
    baseline_batch_id: str | None = None


def create_app(service: QualityService | None = None) -> FastAPI:
    svc = service or QualityService()
    app = FastAPI(title="data-quality-validation", version="0.1.0")

    @app.exception_handler(QualityError)
    async def _handle(_request, exc: QualityError):  # type: ignore[no-untyped-def]
        return _error_response(exc)

    @app.post("/contracts")
    def register_contract(raw: dict[str, Any]) -> dict[str, Any]:
        contract = svc.register_contract(raw)
        return {"registered": contract.name, "version": contract.version,
                "fields": [s.name for s in contract.fields_]}

    @app.post("/contracts/{name}/batches/{batch_id}/validate")
    def validate(name: str, batch_id: str, payload: SubmitRequest) -> dict[str, Any]:
        result = svc.submit(
            name, batch_id, payload.rows,
            baseline_batch_id=payload.baseline_batch_id,
        )
        return {
            "batch_id": result.batch_id,
            "contract": result.contract,
            "passed": result.passed,
            "total_rows": result.total_rows,
            "violation_count": result.violation_count,
            "violations": [v.model_dump() for v in result.violations],
        }

    @app.get("/contracts/{name}/batches/{batch_id}/trace")
    def trace(name: str, batch_id: str) -> dict[str, Any]:
        return svc.trace(name, batch_id)

    @app.get("/contracts/{name}/history")
    def history(name: str) -> dict[str, Any]:
        snaps = svc.history(name)
        return {"contract": name, "metrics": [s.trace() for s in snaps]}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _error_response(exc: QualityError) -> JSONResponse:
    status = _STATUS.get(exc.category, 500)
    return JSONResponse(status_code=status, content={"error": exc.to_dict()})
