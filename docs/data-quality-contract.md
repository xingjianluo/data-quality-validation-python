# 本地数据质量契约：语义、错误分类与指标判定

本服务在同一份数据上，针对不同约束组合与重复/并发执行，给出**确定、一致、
可解释**的质量判定。本文说明契约语义、错误分类、指标与漂移/时效判定规则，
以及验证方法。

## 1. 契约语义

契约用 JSON 定义，注册时由 `normalize_contract` 归一化为统一模型 `Contract`：

```json
{
  "name": "orders",
  "version": 1,
  "fields": [
    {"name": "id",    "type": "integer", "unique": true, "required": true},
    {"name": "amount","type": "number",  "min_value": 0, "max_value": 100000},
    {"name": "status","type": "string",  "allowed": ["paid", "refunded"]},
    {"name": "note",  "type": "string",  "required": false, "nullable": true,
     "min_length": 0, "max_length": 200, "regex": "^[^{}]*$"},
    {"name": "ts",    "type": "timestamp"}
  ],
  "freshness_field": "ts",
  "freshness_max_age_seconds": 3600
}
```

### 1.1 字段约束（统一归一为 `FieldSpec`）

| 约束 | 适用类型 | 含义 |
| --- | --- | --- |
| `type` | 全部 | `string / integer / number / boolean / date / timestamp` |
| `required` | 全部 | 默认 `true`，数据行缺列且未声明可选时判缺失 |
| `nullable` | 全部 | 默认 `false`；为 `true` 时允许显式 `null` |
| `min_value` / `max_value` | `integer/number` | 闭区间取值边界 |
| `allowed` | 与类型兼容 | 枚举白名单，元素必须与声明类型兼容 |
| `min_length` / `max_length` | `string` | 字符串长度闭区间 |
| `regex` | `string` | 必须被 `re.fullmatch` 匹配 |
| `unique` | 全部 | 同一批次内该字段非空值不得重复 |

`date/timestamp` 接收 ISO8601 字符串（时间戳支持尾部 `Z`）或对应原生对象；
`boolean` 与数值严格区分（Python 的 `True/False` 不被当作整数）。

### 1.2 缺失与 null 的语义

* 可选字段（`required=false`）**缺列**：放行；
* 值为 `null`（或缺列）时，只有 `nullable=true` 才放行；
* 必填字段缺列 / 禁止 null 却为 null：判 `DATA_MISSING_FIELD`。

### 1.3 契约非法、冲突、字段缺失都不静默跳过

归一化是“全有或全无”：任何问题都抛出带 `category + reason` 的错误，
不会返回部分可用契约，也不吞掉底层解析器（pydantic）的异常——其原始异常
仅挂在 `cause` 上供排查。

* **非法 `CONTRACT_INVALID`**：name 为空、fields 为空、未知类型、
  长度约束用于非字符串、取值边界用于非数值、白名单元素类型不符、
  freshness 字段类型不是时间类型、SLA 为负等；
* **冲突 `CONSTRAINT_CONFLICT`**：同名字段重复声明、`min>max`（值/长度）、
  空白名单、白名单元素落在 `[min,max]` 或长度区间之外；
* **字段未声明 `FIELD_NOT_DECLARED`**：`freshness_field` 引用了不存在的
  字段；运行时引用未注册契约同样归入此类。

完整的机器可读 `reason` 列表见 `app/quality/contracts.py`。

## 2. 错误分类

| 类别 `category` | 归属 | 典型场景 | HTTP |
| --- | --- | --- | --- |
| `contract_invalid` | 定义侧 | 契约结构/类型语义非法 | 422 |
| `constraint_conflict` | 定义侧 | 约束互相矛盾 | 409 |
| `field_not_declared` | 定义侧 | 引用未声明字段 / 未注册契约 | 404 |
| `data_violation` | 数据侧 | 类型不符、越界、枚举外、正则、唯一性冲突 | 200（结论体内） |
| `data_missing_field` | 数据侧 | 必填缺列 / 禁止 null 却为 null | 200（结论体内） |
| `dependency_failure` | 依赖侧 | 校验依赖自身抛错、时间戳无法解析等 | 503 |
| `metric_not_found` | 查询侧 | 回溯不存在的批次/契约 | 404 |

关键边界：

* **数据不满足期望 ≠ 依赖故障**。数据违规以 `Violation` 列表返回，
  包含 `row / field / rule / expected / actual / category`；
* 底层校验依赖（可注入的 `TypeChecker`）抛出的任意异常都会被
  `ValidationEngine` 捕获并归一为 `DEPENDENCY_FAILURE`，
  对外消息不含内部堆栈，原始异常仅存于 `cause`。

## 3. 指标与历史判定

每个批次在**首次提交时**计算并冻结指标快照 `MetricSnapshot`：

* **完整性 completeness** = 非空必填单元数 / (行数 × 必填字段数)；
* **唯一性 uniqueness** = 去重后非空键数 / 非空键总数（无键记 1.0）；
* **新鲜度 freshness** = 年龄不超过 SLA 的行占比，健康阈值固定 1.0，
  逐行 SLA `max_age_seconds` 一并固化。

每个快照固化 `value / threshold / healthy / evaluated_at / reason /
max_age_seconds`。

### 3.1 漂移与时效阈值变更不得篡改历史

阈值只在批次**首次判定**时读取并写入快照。之后重新注册同名契约的新版本
（收紧/放宽 SLA、调整阈值）：

* 旧批次的快照保持原值、原阈值、原健康结论，不可回写；
* 新批次按新阈值判定。

因此“同一批次无论何时回溯，结论一致”。

### 3.2 从指标回溯到契约、批次与触发原因

`GET /contracts/{name}/batches/{batch}/trace` 返回：

* `contract` / `batch_id`：定位契约与执行批次；
* `validation`：是否通过、行数、违规明细（含每条的判定依据 `rule/expected`）；
* `metrics[]`：每个指标的 `value/threshold/healthy/reason/evaluated_at`，
  异常可直接读到触发原因。

## 4. 并发与重复执行的一致性

幂等键为 `(contract, batch_id)`：

* 提交先走无锁快路径查历史；未命中再进入该批次独占锁并 double-check；
* 同一批次只有第一个线程真正执行校验并落库结果与指标；
* 并发/重复提交者拿到的是**同一首次结论对象**，指标只累计一次；
* 即使重复请求携带不同的数据体，也以首次批次结论为准（批次即幂等键）。

校验引擎本身是无副作用的纯计算，同输入重复执行结果严格相等。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/contracts` | 注册/更新契约（非法即拒绝） |
| POST | `/contracts/{name}/batches/{batch}/validate` | 幂等提交批次校验 |
| GET | `/contracts/{name}/batches/{batch}/trace` | 指标与判定回溯 |
| GET | `/contracts/{name}/history` | 契约全批次指标历史 |
| GET | `/health` | 健康检查 |

## 6. 验证方法

```bash
# 运行全部单测（用例日志会打印 INPUT / EXPECTED / BASIS）
python3 -m pytest -q -s

# 启动服务
uvicorn main:app --reload
```

测试重点（共 30+ 用例）：

* `tests/test_contracts.py`：畸形契约、冲突约束、字段缺失的可区分拒绝；
* `tests/test_engine.py`：数据违规/缺失/依赖故障三类分离、可重复性；
* `tests/test_metrics_and_concurrency.py`：16 线程并发与重复提交只落一次、
  指标不重复累计、阈值变更不篡改历史、trace 回溯；
* `tests/test_api.py`：错误分类到 HTTP 状态码的映射与接口幂等。

每个用例通过 `case` 夹具在日志中打印 `INPUT[...] / EXPECTED / BASIS /
ACTUAL`，便于核对输入、期望与判定依据。
