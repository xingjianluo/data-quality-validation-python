# data-quality-validation-python

本地数据质量契约的校验与监控能力：同一份数据在不同约束组合与重复/并发执行下，
判定结论一致、可解释，错误分类可区分，质量指标可历史回溯。

## 快速开始

```bash
fastapi dev main.py            # 启动服务，默认 http://localhost:8000
python -m pytest tests -q      # 运行全部单测
```

## 能力概览

- **契约定义与校验**：字段类型、必填/可空、取值区间、长度、正则、枚举、唯一
  等约束归一为统一模型；契约畸形（MALFORMED_CONTRACT）、约束冲突
  （CONFLICTING_CONSTRAINTS）、字段缺失（MISSING_FIELD）在执行前被拒绝，
  原因可区分、不静默跳过。
- **校验执行与异常归一化**：数据违约（data_violation，逐项 violations）
  与依赖自身故障（dependency_failure，503，内部异常不透出）严格分类；
  并发/重复提交只执行一次，结论一致、指标不重复累计。
- **指标历史与判定**：完整性、唯一性、新鲜度与完整性漂移；
  快照只追加、阈值随快照固化，事后改阈值不篡改历史；
  每个指标可回溯到契约、版本、执行批次与触发原因。

## 模块结构

```
dqv/
  errors.py      # 三类互斥错误分类与拒绝原因
  contracts.py   # 契约解析、约束归一化、冲突/畸形/缺失检测
  validators.py  # 逐单元格+跨行校验，依赖异常边界归一化
  metrics.py     # 指标计算、漂移/时效判定、只追加历史库
  engine.py      # 幂等键/内容指纹去重、并发单次执行
  service.py     # FastAPI 路由与错误->HTTP 映射
tests/           # 以畸形契约/冲突约束/并发重复执行为主，日志含输入/期望/判定依据
docs/quality-contracts.md  # 契约语义、错误分类、指标规则、验证方法（详细文档）
```

## 错误分类

| 类别 | 含义 | HTTP |
|------|------|------|
| contract_rejected | 契约非法/冲突/缺字段（附 reason、details） | 422 |
| data_violation | 数据不满足契约（附逐项 violations） | 200 `passed=false` / 409 重放冲突 |
| dependency_failure | 校验依赖自身故障（已归一化，不透出内部异常） | 503 |

完整语义与指标判定规则见 [docs/quality-contracts.md](docs/quality-contracts.md)。
