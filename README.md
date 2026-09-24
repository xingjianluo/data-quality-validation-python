# data-quality-validation

本地数据质量契约的校验与监控服务：统一契约模型、可区分的错误分类、
并发/重复执行下确定一致的判定，以及完整性/唯一性/新鲜度指标的可回溯历史。

## 快速开始

```bash
uv sync                       # 或使用现有环境
uvicorn main:app --reload     # http://127.0.0.1:8000/docs
python3 -m pytest -q -s       # 运行测试并查看用例判定日志
```

## 能力一览

- **契约定义与校验**：类型、必填、可空、取值边界、枚举、长度、正则、唯一性
  归一为统一 `FieldSpec`；契约非法、约束冲突、字段缺失一律带 `category+reason`
  拒绝，不静默跳过。
- **执行与异常归一化**：数据不满足约束（`data_violation` /
  `data_missing_field`）与依赖自身故障（`dependency_failure`）分为不同类别，
  依赖原始异常不直接透出。
- **幂等并发**：以 `(contract, batch_id)` 为幂等键，并发/重复提交返回同一首次
  结论，指标只累计一次。
- **指标历史**：完整性、唯一性、新鲜度快照在批次首次判定时冻结；阈值/SLA 变更
  不回写历史；可经 trace 回溯到契约、批次与触发原因。

## 模块结构

```
app/quality/
  errors.py     # 错误大类与归一化异常
  contracts.py  # 契约统一模型、归一化与非法/冲突拒绝
  engine.py     # 校验引擎、依赖异常归一化、唯一性
  metrics.py    # 完整性/唯一性/新鲜度的冻结快照
  storage.py    # 线程安全、幂等的本地留存
  service.py    # 编排：注册、幂等提交、指标判定、回溯
  api.py        # FastAPI 路由与错误码映射
tests/          # 畸形契约、冲突约束、并发重复执行为主的测试
docs/data-quality-contract.md  # 语义、错误分类、指标与判定规则
```

完整语义与错误码表见 [`docs/data-quality-contract.md`](docs/data-quality-contract.md)。
