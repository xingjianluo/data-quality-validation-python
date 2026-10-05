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
  "freshness_max_age_seconds": 3600,
  "drift_tolerance": {"completeness": 0.0, "uniqueness": 0.01, "freshness": 0.0}
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

### 1.2 契约级配置项

| 配置项 | 含义 |
| --- | --- |
| `name` / `version` | 契约名与整数版本号（`>=1`，默认 1） |
| `fields` | 至少一个字段定义 |
| `freshness_field` / `freshness_max_age_seconds` | 新鲜度时间字段与逐行 SLA（秒） |
| `drift_tolerance` | 漂移容忍幅度：`{completeness?, uniqueness?, freshness?}`，每个值为 `[0,1]` 的"允许最大下滑幅度"，缺省视为 `0.0`（不允许下滑） |

### 1.3 缺失与 null 的语义

* 可选字段（`required=false`）**缺列**：放行；
* 值为 `null`（或缺列）时，只有 `nullable=true` 才放行；
* 必填字段缺列 / 禁止 null 却为 null：判 `DATA_MISSING_FIELD`。

### 1.4 严格注册：不认识的配置项一律拒绝

注册是"严格白名单"模式，系统不认识的配置项**不会被当成没写而静默放过**，
统一以 `CONTRACT_INVALID` 拒绝，并在消息里定位具体位置：

* 契约顶层出现白名单外的键 -> `reason=unknown_contract_key`
  （`field` 为该键名，消息标注"契约顶层"），如把 `freshness_field`
  拼成 `freshnes_field`，或凭空写一个 `threshold`；
* 字段定义里出现白名单外的键 -> `reason=unknown_field_key`
  （`field` 为该键名，消息标注 `fields[i].<键>`），如把 `unique`
  拼成 `uniqe`、`nullable` 拼成 `nulable`；
* `drift_tolerance` 里写了不认识的指标名 -> `reason=unknown_drift_metric`；
  不是对象 -> `drift_tolerance_not_object`；值不是数值 / 越界 ->
  `drift_tolerance_not_number` / `drift_tolerance_out_of_range`。

### 1.5 契约非法、冲突、字段缺失都不静默跳过

归一化是“全有或全无”：任何问题都抛出带 `category + reason` 的错误，
不会返回部分可用契约，也不吞掉底层解析器（pydantic）的异常——其原始异常
仅挂在 `cause` 上供排查。模型同时开启 `extra="forbid"` 作为第二道防线。

* **非法 `CONTRACT_INVALID`**：name 为空、fields 为空、未知类型、
  未知配置项（见 1.4）、长度约束用于非字符串、取值边界用于非数值、
  白名单元素类型不符、freshness 字段类型不是时间类型、SLA 为负等；
* **冲突 `CONSTRAINT_CONFLICT`**：同名字段重复声明、`min>max`（值/长度）、
  空白名单、白名单元素落在 `[min,max]` 或长度区间之外；
* **字段未声明 `FIELD_NOT_DECLARED`**：`freshness_field` 引用了不存在的
  字段；运行时引用未注册契约同样归入此类。

完整的机器可读 `reason` 列表见 `app/quality/contracts.py`。

### 1.6 全可选字段契约是合法的

允许一份契约的**所有字段都可选**（例如只做去重与时效观察的表，
没有任何必填列）。这类契约注册合法，提交数据时校验照常返回结论；
其完整性取值见 3.0，不会一提交就报错。

## 2. 错误分类

| 类别 `category` | 归属 | 典型场景 | HTTP |
| --- | --- | --- | --- |
| `contract_invalid` | 定义侧 | 契约结构/类型语义非法 | 422 |
| `constraint_conflict` | 定义侧 | 约束互相矛盾 | 409 |
| `field_not_declared` | 定义侧 | 引用未声明字段 / 未注册契约 | 404 |
| `data_violation` | 数据侧 | 类型不符、越界、枚举外、正则、唯一性冲突 | 200（结论体内） |
| `data_missing_field` | 数据侧 | 必填缺列 / 禁止 null 却为 null | 200（结论体内） |
| `dependency_failure` | 依赖侧 | 校验依赖自身抛错、时间戳无法解析等 | 503 |
| `metric_not_found` | 查询侧 | 回溯不存在的批次/契约；显式漂移基线不存在或未判定 | 404 |

关键边界：

* **数据不满足期望 ≠ 依赖故障**。数据违规以 `Violation` 列表返回，
  包含 `row / field / rule / expected / actual / category`；
* 底层校验依赖（可注入的 `TypeChecker`）抛出的任意异常都会被
  `ValidationEngine` 捕获并归一为 `DEPENDENCY_FAILURE`，
  对外消息不含内部堆栈，原始异常仅存于 `cause`。

## 3. 指标与历史判定

每个批次在**首次提交时**计算并冻结指标快照 `MetricSnapshot`：

* **完整性 completeness** = 非空必填单元数 / (行数 × 必填字段数)；
* **唯一性 uniqueness** = 去重后非空键数 / 非空键总数（无键记 1.0），
  快照带 `dimension`（被观察的字段名），漂移据此配对；
* **新鲜度 freshness** = 年龄不超过 SLA 的行占比，健康阈值固定 1.0，
  逐行 SLA `max_age_seconds` 一并固化。

每个快照固化 `value / threshold / healthy / evaluated_at / reason /
max_age_seconds / dimension`。

### 3.0 全可选字段时完整性怎么算

当契约没有任何必填字段时，完整性不存在“缺失”的可能，取值依据明确记为
**1.0**（无必填单元需要满足，定义上 vacuously complete），`healthy=true`，
`reason` 写明“契约无必填字段，完整性按 1.0 计（无必填单元可缺失）”。
空批次同样取 1.0，而不是报错或硬凑 0。唯一性、新鲜度仍按各自规则照常计算，
因此全可选契约的批次在 trace 里依然能看到完整的指标集合。

### 3.1 漂移：当前批次与基线批次对比

漂移回答的是“跟过去比，质量有没有明显退步”。批次首次判定时，把当前批次
的指标快照与一个**基线批次**逐项对比，产出不可变的 `DriftVerdict`。

**基线怎么定：**

* 默认（`baseline_batch_id` 不传）：取判定顺序上**最近一次已判定**的
  批次（`baseline_mode=latest`）；
* 使用方显式指定：提交体带 `baseline_batch_id`，与该更早的历史批次比
  （`baseline_mode=explicit`）；
* 首个批次没有任何已判定基线 -> 不硬凑结论，trace 中 `drift=null`。

**怎么算退步：** 指标按 `(metric, dimension)` 配对（完整性/新鲜度维度为
空，唯一性维度为字段名）；只对比基线与当前都存在的指标。对每个指标取
下滑量 `decline = baseline_value - current_value`，当
`decline > drift_tolerance[metric]`（缺省 0.0）时该指标判退步；**任一
指标退步，整批 `status=regressed`**，否则 `within_tolerance`。每个指标都
带 `baseline_value / current_value / decline / tolerance / regressed /
reason`，总 `reason` 汇总是哪些指标超幅度，能说清“跟谁比、按什么幅度、
为什么算退步”。

**基线不可用时明确拒绝，不硬凑：** 显式基线批次不存在、尚未判定完成，或
基线就是当前批次自身时，提交以 `METRIC_NOT_FOUND` 拒绝：

* `reason=baseline_batch_not_judged`（`batch` 为该基线批次）；
* `reason=baseline_is_current_batch`。

拒绝时**不产生任何落库**（该批次不会变成已判定批次），调用方可改正基线
后用同一批次 id 重试。

### 3.2 漂移与阈值在首次判定时固化

漂移结论只在批次**第一次判定时算一次并固定下来**：基线批次、基线模式、
当时生效的每指标容忍幅度、各指标下滑量与总判全部写入 `DriftVerdict`。
此后：

* 再来新批次，只影响新批次自己的判定；
* 调整 `drift_tolerance` 或 SLA、重新注册同名契约的新版本，都**不会改写**
  已判定老批次的基线、幅度与结论；
* 同一批次以后任何时候回溯，`drift` 都与当时一致。

基线解析 + 漂移判定 + 落库在契约级判定锁内串行，配合批次锁与
double-check，保证并发的不同批次“最近基线”确定有序，且每个批次只判定
一次。

### 3.3 从指标回溯到契约、批次与触发原因

`GET /contracts/{name}/batches/{batch}/trace` 返回：

* `contract` / `batch_id`：定位契约与执行批次；
* `validation`：是否通过、行数、违规明细（含每条的判定依据 `rule/expected`）；
* `metrics[]`：每个指标的 `value/threshold/healthy/reason/evaluated_at`，
  异常可直接读到触发原因；
* `drift`：**追加字段**，漂移结论（首个无基线批次为 `null`）。老字段含义
  与结构保持不变。

## 4. 并发与重复执行的一致性

幂等键为 `(contract, batch_id)`：

* 提交先走无锁快路径查历史；未命中再进入该批次独占锁并 double-check；
* 同一批次只有第一个线程真正执行校验，并在契约级判定锁内解析基线、
  判定漂移、原子落库结果 + 指标 + 漂移结论；
* 并发/重复提交者拿到的是**同一首次结论对象**，指标只累计一次、
  漂移只判定一次（重复提交即使携带不同数据或不同基线参数也被忽略）；
* 不同批次并发时，“最近一次已判定批次”这一基线在契约级锁内确定有序，
  不会两个批次选中同一个或互相抢占。

校验引擎本身是无副作用的纯计算，同输入重复执行结果严格相等。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/contracts` | 注册/更新契约（非法或含未知配置项即 422/409 拒绝） |
| POST | `/contracts/{name}/batches/{batch}/validate` | 幂等提交批次校验；请求体可选 `baseline_batch_id` 指定漂移基线 |
| GET | `/contracts/{name}/batches/{batch}/trace` | 指标与判定回溯（含追加字段 `drift`） |
| GET | `/contracts/{name}/history` | 契约全批次指标历史 |
| GET | `/health` | 健康检查 |

提交体示例：

```json
{"batch_id": "B2", "rows": [{"id": 1}], "baseline_batch_id": "B1"}
```

## 6. 验证方法

```bash
# 运行全部单测（用例日志会打印 INPUT / EXPECTED / BASIS）
python3 -m pytest -q -s

# 启动服务
uvicorn main:app --reload
```

测试覆盖：

* `tests/test_contracts.py`：畸形契约、冲突约束、字段缺失的可区分拒绝；
* `tests/test_strict_contracts.py`：顶层/字段级未知配置项（含约束名拼错）、
  漂移配置非法指标名/形状/越界一律拒绝，并定位位置；已知键不被误伤；
* `tests/test_engine.py`：数据违规/缺失/依赖故障三类分离、可重复性；
* `tests/test_metrics_and_concurrency.py`：16 线程并发与重复提交只落一次、
  指标不重复累计、阈值变更不篡改历史、trace 回溯；
* `tests/test_drift.py`：漂移单元判定（超/不超容忍）、最近基线与显式基线、
  基线不存在/未判定/指向自身被拒绝且不落库、结论随首次判定固化（新批次、
  收紧容忍、重注册新版本都不改写老批次）、并发/重复不重复判定与累计、
  全可选字段契约正常出结论且完整性按 1.0 计；
* `tests/test_drift_api.py`：显式基线透传、基线缺失 404、未知配置项 422、
  trace 追加 `drift` 且老字段不变；
* `tests/test_api.py`：错误分类到 HTTP 状态码的映射与接口幂等。

每个用例通过 `case` 夹具在日志中打印 `INPUT[...] / EXPECTED / BASIS /
ACTUAL`，便于核对输入、期望与判定依据。
