"""FastAPI 服务装配：契约校验、数据校验提交、指标历史查询。

错误到响应的映射（三类错误可区分，绝不 200 静默跳过）：
* ContractRejectedError -> 422，body 含 reason 与 details；
* DataValidationError   -> 409（提交冲突/内容不匹配）；
* DependencyFailureError -> 503（依赖故障，只暴露归一化信息）；
数据不满足契约属于正常校验结论 -> 200，body.passed=false 且附 violations。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dqv.contracts import parse_contract
from dqv.engine import ValidationEngine
from dqv.errors import (
    ContractRejectedError,
    DataValidationError,
    DependencyFailureError,
    ErrorCategory,
)

app = FastAPI(title="data-quality-validation", version="0.1.0")
engine = ValidationEngine()


class SubmitRequest(BaseModel):
    contract: dict[str, Any]
    records: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None
    batch_id: str | None = None
    now_epoch: float | None = None


class ValidateContractRequest(BaseModel):
    contract: dict[str, Any]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/contracts/validate")
def validate_contract(req: ValidateContractRequest) -> dict[str, object]:
    """仅验证契约本身是否合法；非法契约返回 422 与可区分的拒绝原因。"""
    parsed = parse_contract(req.contract)
    return {
        "accepted": True,
        "contract_name": parsed.name,
        "contract_version": parsed.version,
        "fields": [f.name for f in parsed.fields],
    }


@app.post("/validations")
def submit_validation(req: SubmitRequest) -> dict[str, object]:
    result = engine.submit(
        req.contract,
        req.records,
        idempotency_key=req.idempotency_key,
        batch_id=req.batch_id,
        now_epoch=req.now_epoch,
    )
    payload = result.to_dict()
    payload["metrics"] = [
        s.to_dict()
        for s in engine.metrics.history(result.contract_name)
        if s.batch_id == result.batch_id
    ]
    return payload


@app.get("/contracts/{contract_name}/metrics")
def get_metrics(contract_name: str) -> dict[str, object]:
    history = engine.metrics.history(contract_name)
    if not history:
        return JSONResponse(
            status_code=404,
            content={"category": ErrorCategory.DATA_VIOLATION.value,
                     "message": f"未找到契约 {contract_name} 的任何指标历史"},
        )
    return {
        "contract_name": contract_name,
        "count": len(history),
        "snapshots": [s.to_dict() for s in history],
    }


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, object]:
    result = engine.get_result(batch_id)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"category": ErrorCategory.DATA_VIOLATION.value,
                     "message": f"批次 {batch_id} 不存在"},
        )
    return result.to_dict()


@app.exception_handler(ContractRejectedError)
def _on_contract_rejected(_request: Any, exc: ContractRejectedError) -> JSONResponse:
    return JSONResponse(status_code=422, content=exc.to_dict())


@app.exception_handler(DataValidationError)
def _on_data_validation(_request: Any, exc: DataValidationError) -> JSONResponse:
    return JSONResponse(status_code=409, content=exc.to_dict())


@app.exception_handler(DependencyFailureError)
def _on_dependency_failure(_request: Any, exc: DependencyFailureError) -> JSONResponse:
    return JSONResponse(status_code=503, content=exc.to_dict())
