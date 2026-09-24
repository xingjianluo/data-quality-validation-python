# 本地数据质量契约：校验与监控说明

本文档说明数据契约语义、错误分类、质量指标与漂移/时效判定规则，以及验证方法。

## 1. 契约语义

契约用 JSON 对象（或等价 dict）描述，解析后归一为不可变的 `Contract`：

```json
{
  "name": "orders",
  "version": 3,
  "freshness_max_age_seconds": 100,
  "fields": [
    {"name": "id", "type": "integer", "constraints": {
      "required": true, "nullable": false, "unique": true,
      "min_value": 1, "max_value": 1000000}},
    {"name": "status", "type": "string", "constraints": {
      "required": false, "nullable": true,
      "min_length": 2, "max_length": 16,
      "pattern": "^[a-z]+$",
      "allowed_values": ["new", "paid"]}}
  ]
}
```

### 1.1 字段类型（归一化）

| type      | 接受的 Python/JSON 值 |
|-----------|----------------------|
| `string`  | 字符串（不接受数字隐式转换） |
| `integer` | int，**不**接受 bool |
| `number`  | int/float，**不**接受 bool |
| `boolean` | true/false |
| `date`    | `YYYY-MM-DD` 字符串 |
| `timestamp` | epoch 数值或 ISO-8601 字符串 |

### 1.2 统一约束模型

所有字段约束归一为同一模型 `Constraints`：

| 约束 | 含义 | 适用类型 |
|------|------|---------|
| `required`（默认 true） | 字段键必须出现 | 全部 |
| `nullable`（默认 false） | 值允许为 `null` | 全部 |
| `min_value` / `max_value` | 数值闭区间 | integer/number |
| `min_length` / `max_length` | 字符串长度区间 | string |
| `pattern` | 必须被正则 `fullmatch` | string |
| `allowed_values` | 枚举，元素必须与声明类型兼容 | 全部 |
| `unique` | 同列值跨行唯一（null 不参与比较） | 全部 |

顶层 `freshness_max_age_seconds` 为正数时启用新鲜度判定（见 3.3）。

### 1.3 契约受理规则（绝不静默跳过）

解析按固定顺序检测，同一轮的多个问题聚合在 `details` 中一次性返回：

1. **MISSING_FIELD**：`fields` 缺失；字段条目缺 `name` 或 `type`。
2. **MALFORMED_CONTRACT**：JSON 不可解析、顶层不是对象、类型未知、
   未知键、坏正则、约束类型不匹配（如 string 上写 `min_value`）、
   `version<1`、空字段列表、枚举元素与类型不兼容等。
3. **CONFLICTING_CONSTRAINTS**：约束组合为空集或相互矛盾：
   `min_value>max_value`、`min_length>max_length`、
   枚举与数值区间无交集、枚举与正则无交集、字段重名。

非法契约在执行数据校验**之前**被拒绝：不产生执行批次，也不写任何指标。

## 2. 错误分类

三类错误互斥、可区分；底层依赖异常永远不会被直接透出。

| 类别 | 何时产生 | 携带信息 | HTTP |
|------|---------|---------|------|
| `contract_rejected` | 契约不可受理（见 1.3） | `reason` + `details[]` | 422 |
| `data_violation` | 契约合法、依赖正常，但数据不满足期望 | 逐项 `violations` | 200（`passed=false`，属正常结论）/ 409（同批次提交了不同内容） |
| `dependency_failure` | 校验依赖自身故障（检查器抛异常等） | 归一化 message + `component`，**不含**内部堆栈/路径 | 503 |

数据违约码（`ViolationCode`）：`required_missing`、`null_not_allowed`、
`type_mismatch`、`value_out_of_range`、`length_out_of_range`、
`regex_mismatch`、`not_in_allowed_values`、`duplicate_key`。

每条违约记录五要素，保证可解释：

```
row_index（行号） / field_name（字段） / code（违约码）
expected（期望）   / actual（实际值）   / basis（判定依据：声明类型+约束+后端结论）
```

依赖异常归一化示例：检查器内部抛出
`RuntimeError("secrets ... /tmp/x")`，对外只得到
`类型/取值检查依赖发生内部故障（细节已归一化）`，category=dependency_failure，
内部消息不离开进程边界。

## 3. 质量指标与历史判定

每次提交产生不可变快照 `MetricSnapshot`，字段包括：
`batch_id, contract_name, contract_version, metric, value, threshold,
passed, reason, evaluated_at`。

- **只追加**：历史库 append-only；同一 `(batch_id, metric)` 重复写入会被拒绝，
  是并发/重复提交“指标不重复累计”的最后防线。
- **阈值随快照固化**：漂移/新鲜度/完整性阈值变更只影响之后的批次，
  历史快照的 `threshold/passed/reason` 绝不重算或篡改。
- **可回溯**：任何指标都带契约名、契约版本、批次 ID 与触发原因，
  可经 `/batches/{id}` 与 `/contracts/{name}/metrics` 双向回溯。

### 3.1 完整性 completeness

```
completeness = 非空且出现的单元格数 / (行数 × 契约字段数)
```

判定：`completeness >= completeness_min`（默认 1.0）。空数据集按 1.0 计。

### 3.2 唯一性 uniqueness

对所有 `unique=True` 字段计算 `去重后非空值数 / 非空值数`，取最小值。
未声明唯一字段时按 1.0 计（无约束对象）。判定：`uniqueness >= uniqueness_min`。

### 3.3 新鲜度 freshness

仅当契约声明 `freshness_max_age_seconds` 时产生快照：

```
age = now_epoch - max(所有 timestamp 字段的最大时间戳)
passed = age <= freshness_max_age_seconds
```

- 找不到任何可解析 timestamp 时 `age=+inf`、判定失败（不得静默通过）。
- 服务端可在提交时显式传入 `now_epoch`，保证重复执行与测试可复现。

### 3.4 完整性漂移 completeness_drift

```
drop = 上一批次完整性 - 本批次完整性
passed = drop <= drift_max_drop（默认 0.0）
首批数据无基线：drop=0 且直接通过，reason 记录“首批数据”。
```

基线取历史库中该契约**最近一次**完整性快照；阈值改动不会改变基线快照内容。

## 4. 执行、幂等与并发一致性

- 每次提交由单一标识定位，既是幂等键也是执行批次 ID：
  `batch_id` > `idempotency_key` > 内容指纹
  （指纹 = 规范化后的契约+数据的 sha256 前 16 位）。
- 同一标识的**并发**提交：只有 leader 线程执行一次，其余线程等待
  `threading.Event` 并共享同一份 `ValidationResult`；指标只累计一次。
- 同一标识的**重复**提交：直接返回首次结果，不重新校验、不追加指标。
- 同一标识但内容不同：拒绝（409/data_violation），防止把新执行伪装成重放。
- 校验结论除 `evaluated_at` 外有稳定指纹 `signature()`，
  同输入重复执行的 `passed` 与全部违约项完全一致。

## 5. HTTP 接口

| 方法/路径 | 用途 |
|-----------|------|
| `GET /health` | 健康检查 |
| `POST /contracts/validate` | 仅校验契约本身；非法返回 422 + reason |
| `POST /validations` | 提交校验（请求体：contract/records/idempotency_key/batch_id/now_epoch）；响应含 violations 与本批次指标 |
| `GET /batches/{batch_id}` | 按批次回溯校验结论 |
| `GET /contracts/{name}/metrics` | 按契约回溯全部指标快照 |

## 6. 验证方法

```bash
# 运行全部单测（日志打印每条用例的输入/期望/判定依据）
python -m pytest tests -q --log-cli-level=INFO

# 启动服务
fastapi dev main.py        # 或 python -m uvicorn main:app --reload
```

快速手工验证：

```bash
curl -s localhost:8000/contracts/validate -H 'content-type: application/json' \
  -d '{"contract":{"name":"x","fields":[]}}'
# -> 422 {"category":"contract_rejected","reason":"malformed_contract", ...}
```

测试组织（畸形契约/冲突约束/并发重复执行为主）：

| 文件 | 覆盖点 |
|------|--------|
| `tests/test_contracts_rejection.py` | 三类拒绝原因可区分、问题聚合 |
| `tests/test_validators.py` | 八种违约码、依赖故障 vs 数据违约 |
| `tests/test_engine_idempotency.py` | 16 线程并发同键、重复提交、指纹去重、拒绝不留痕 |
| `tests/test_metrics_history.py` | 完整性/唯一性/新鲜度/漂移、阈值变更不篡改历史、回溯字段 |
| `tests/test_service_api.py` | HTTP 状态码分类、幂等响应一致、批次/指标回溯 |
