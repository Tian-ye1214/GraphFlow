# 批次十七：HTTP 取数节点 + 列可见性 UX 设计

**日期：** 2026-06-17
**分支：** `feat/http-fetch-node`

贯穿约束（KISS 硬规则）：最简实现、不预防未发生的 bug；模型 `api_key` 全程 Fernet 加密、绝不进响应/日志/提示词；所有模型/工作流/运行/数据集引用校验 `user_id`（租户隔离）；尽量不加表不加列（沿用既有 JSON config 结构，无 migration）。

三项需求（用户原话归纳）：
1. 跑数任务的中间链路有不少环节需要**调接口获取数据**——决定**加一个新节点**（而非复用自动处理节点）。
2. 输入 Excel 不一定每列都用上——节点要用**绿色标记 LLM 能看到的列**。
3. 输入列可能很多——前端用**下拉框展示全部列、不省略**。

---

## 决策记录（已与用户敲定）

| 议题 | 决策 | 理由 |
|---|---|---|
| 加新节点 vs 复用 `auto_process` | **加新节点 `http_fetch`** | `auto_process` 是整批同步 barrier（`row_idx=0`），无逐行隔离/并发/进度/重试；按行调接口取数需要这些，应照 `llm_synth` 的逐行模式 |
| 取数粒度 | **v1 逐行取数**；整节点取一次 + 按键 merge 留 **v2** | 逐行覆盖「中间链路按行调接口补数据」主场景；分期交付，先把逐行做扎实 |
| 响应落列 | **JSON 路径提取**：`extract: {列名: json路径}` | 声明式、常用字段直接成列；需要一个小的 `json_path_get` 取值函数 |
| 鉴权 | **内联 Header**（值支持 `{{列}}`，token 明文存 `graph_json`） | KISS，不引入新凭据存储；安全边界见 ④ |
| HTTP 方法 | **v1 仅 GET + POST** | 覆盖绝大多数取数；其余 method 留后续 |

---

## ① 新节点类型 `http_fetch`（逐行，v1）

图里的普通中间节点，上下游沿 normal 边随意连。**输出行数不变**，每行追加 `extract` 声明的列。

- **类型注册：** `engine/graph.py:4` `NODE_TYPES` 加 `"http_fetch"`（不加则 `validate_graph` 拒绝）。
- **派发：** `runner._execute`（runner.py:84-91）加 `elif node.type=="http_fetch": await _run_http_node(...)`。
- **handler：** 新增 `_run_http_node`，**照 `_run_llm_node`（runner.py:235-276）写**——逐行 `RunRow`、`Semaphore(config.concurrency, 默认 4)`、`_cancellable`、`_write_unit`/`_set_node_state`、跳过已 done/failed 行（断点续跑）。token 计数写 0（`_finish` 求和不受影响）。
- **不在 qc rescan 反馈边路径**，无需 rescan 兼容逻辑。

---

## ② config 形状 + JSON 路径

```jsonc
{
  "method": "GET",                                   // v1: GET | POST
  "url": "https://api.x.com/weather?q={{city}}",     // 模板，{{列}} 渲染
  "headers": {                                       // 可选，值支持 {{列}}
    "Authorization": "Bearer sk-xxx",
    "X-City": "{{city}}"
  },
  "body": "{\"q\": \"{{city}}\"}",                   // 仅 POST；模板；可空
  "extract": {                                       // 列名 → JSON 路径
    "temp": "data.temp",
    "desc": "data.weather.0.desc"
  },
  "concurrency": 4, "retries": 2, "timeout": 30      // 复用 llm 同款默认
}
```

**`json_path_get(obj, "data.weather.0.desc")`**（约 10 行，放 `engine/nodes.py` 或小 util）：点号分隔，逐级下钻；数字段对 list 当索引、对 dict 当键；任一级缺失返回 `None`。与 `render_template` 缺失语义一致（`None` → 空串）。不支持通配/过滤（YAGNI）。

---

## ③ 执行 + 错误三态语义

逐行 worker `run_http_fetch_row(config, row, ...)`（放 `engine/nodes.py`，对照 `run_llm_synth_row`）：

1. `render_template` 渲染 `url`、各 `headers` 值、`body`（复用既有单遍渲染，`{{列}}` 不二次展开）。
2. 调 **新增 `app/services/http.py`** 的 `fetch(method, url, headers, body, timeout, retries)` → `(status, text)`。
3. 解析响应 JSON。
4. 对 `extract` 每个 `{列: 路径}` 用 `json_path_get` 取值落列（缺字段落空串）。
5. 返回 `([{**row, **extracted}], {})`（usage 空 → token 计 0）。

**错误三态：**

| 情形 | 处理 |
|---|---|
| 请求失败（超时 / 连接错 / 非 2xx，重试耗尽） | 该行 **failed**（逐行隔离不连坐，可 `rerun-failed`） |
| 响应非 JSON / 解析失败 | 该行 **failed**，`error` 记原因 |
| JSON 有效但某 `extract` 路径缺字段 | 该列落**空串**（同 `render_template` 缺失语义，不算失败） |

即：**请求级失败 = 可见的 failed（不静默落库，承接历轮「空补全静默落库」教训）；字段级缺失 = 空串。**

---

## ④ 安全边界

内联 Header 意味着 token **明文存在 `graph_json`**——知情的 KISS 取舍。守住的红线：

- `graph_json` 按 `user_id` 隔离，他人看不到（租户红线不破）。
- **error / 日志 / SSE 一律不回显 `headers` 值**（防 token 外泄，硬约束）。`HTTPFetchError` 与该行 `error` 文案只含 method/url/status，**绝不含 headers**。
- 与「模型 `api_key` 必须 Fernet 加密」红线**不冲突**：那条针对 `ModelConfig.api_key`；HTTP 节点 token 是用户自填进工作流配置、对配置者本人可见，走明文是其选择。日后要加密走 v2「加密凭据」。

---

## ⑤ 列血缘

- `engine/columns.py:_node_output`（44-59）加分支：`http_fetch` 的 `output = input ∪ extract.keys()`（保持出现顺序、去重）。
- 前端 `NodeConfigForm.tsx:liveOutput()`（41-57）同步加 `http_fetch` 分支（客户端实时重算用）。
- `routers/runs.py` create_run **无需**为 `http_fetch` 加资源 ownership 校验分支——它不引用 `dataset_ids`/`model_config_id`/`judge_model_ids` 等租户资源。

---

## ⑥ 列可见性 UX

### 6.1 绿标 LLM 可见列（纯前端，零后端）

`NodeConfigForm.tsx` 的 `ColumnsBar`（59-79）已有同款正则 `TPL_RE`（20）+ `missingCols`（21）。新增 `referencedCols(text, inputCols)` 助手（`refs ∩ inputCols`），扫 `config.system_prompt` **和** `config.user_prompt`：

| 列状态 | 标记 |
|---|---|
| 被 `{{}}` 引用且在 input 中（LLM 看得到） | **绿**（`color="green"`） |
| 在 input 中但未被引用 | 默认灰 |
| 被引用但上游没有 | 保留现有**红色缺失警告**（`MissingColsWarning`，29-37，不动） |

作用于 `llm_synth` 与 `qc`（共用 `ColumnsBar`）。`referenced` 集从 `NodeConfigForm` 默认导出（480-512）算出后下传给 `ColumnsBar`。

同一 `referencedCols` 逻辑天然复用于 `http_fetch`——扫其 `url` + 各 `headers` 值 + `body` 里的 `{{列}}`，把该节点实际用到的列标绿。实现零额外成本，语义统一为「**绿 = 本节点用到的列**」（用户「不是每列都用上、标出用上的」意图）。

### 6.2 列多→下拉框展示全部（不省略）

`ColumnsBar` 当前把 input 列平铺成可点 chip（并未省略，但列一多就很长）。改为：

- 列数 **> 12**（阈值）时改用 antd `<Select>`（已 import）：`options = inputCols.map(c => ({value:c,label:c}))`，弹层可滚动 + 可搜索、**展示全部不省略**，选中即把 `{{列}}` 插入当前节点的目标字段（沿用现 `onInsert` 路径——`llm_synth`/`qc` 插 User Prompt，`http_fetch` 插当前聚焦的 url/headers/body）。
- 列数 ≤ 12 时保留现 chips 点选 UX。
- 下拉项同样按 6.1 给被引用列加绿色标识（option 渲染带绿点/绿字）。

---

## 数据流与边界

- **租户隔离：** `http_fetch` 不引用数据集/模型；工作流本身已按 `wf.user_id` 校验。无新增跨租户面。
- **不加表不加列：** 仅在 `NODE_TYPES` 加一个字面量、新增一个 service 文件 + 一个 handler + 一个 worker + 一个路径函数，节点配置走既有 JSON config。无 DB migration。
- **空/异常：** `extract` 为空 → 只发请求不落新列（合法，用于纯触发）；`url` 渲染后为空/非法 → 该行 failed。

---

## v2 留观（本批不做，YAGNI）

- 整节点取一次 + 按键 merge（取字典表场景）——加一个「整节点」模式开关，走 barrier 分支 + `_merge_branches` 同款按行/按键合并。
- 加密凭据：复用 `ModelConfig` + Fernet 或新建 credential 模型，替代明文 header。
- 更多 HTTP 方法（PUT/PATCH/DELETE）、非 JSON 响应（整存一列）、JSON 路径通配。

**非目标：** 不做运行期对接口返回结构的强校验；不做请求级缓存/限流（需要时再说）。

---

## 测试策略

后端（pytest，隔离库 + monkeypatch 假 http client，照现有引擎测试风格）：

- `test_runner.py` / 新 `test_http_node.py`：逐行取数落列、并发、**单行失败不连坐整 run**、断点续跑、错误三态（请求失败→failed / 非 JSON→failed / 字段缺失→空串）。
- **安全断言：** 请求失败时该行 `error` 与抛出的 `HTTPFetchError` **不含 `Authorization`/headers 值**。
- `json_path_get` 单测：点号下钻、数组索引、缺级返回 None。
- `test_columns.py`：`http_fetch` 的 `output == input ∪ extract.keys()`、顺序去重、下游 input 含新列。
- `test_cli.py`：`gf node add http_fetch ...` 可建、`gf node show` 摘要正确。

前端：`npm run build`（tsc -b && vite build）+ `npx vitest run` 保绿；`serialize.test.ts` 覆盖新类型。

---

## 文件清单

**后端：**
- 改 `app/engine/graph.py`（`NODE_TYPES` 加 `http_fetch`）。
- 改 `app/engine/runner.py`（`_execute` 派发分支 + 新 `_run_http_node`，照 `_run_llm_node`）。
- 改 `app/engine/nodes.py`（新 `run_http_fetch_row` + `json_path_get`）。
- 新增 `app/services/http.py`（`httpx.AsyncClient` 缓存 + retry/backoff/timeout + `HTTPFetchError`，照 `llm.py` 模式；error 文案不含 headers）。
- 改 `app/engine/columns.py`（`_node_output` 加 `http_fetch` 分支）。
- 改 `app/cli.py`（`NODE_TYPES` 别名 / `NODE_LABELS` / `_node_summary` 同步加 `http_fetch`）。

**前端：**
- 改 `src/api/types.ts`（`GraphNode['type']` union 加 `'http_fetch'`）。
- 改 `src/canvas/serialize.ts`（`NODE_LABELS` 加 `http_fetch` → 自动进 palette）。
- 改 `src/canvas/nodeTypes.tsx`（`COLORS` + `nodeTypes` registry 加 `http_fetch`）。
- 改 `src/canvas/forms/NodeConfigForm.tsx`：
  - 新 `HttpFetchForm`（method/url/headers/body/extract 编辑器 + 复用 `ColumnsBar` 往 url/headers/body 插 `{{列}}`）+ switch case + `liveOutput()` 分支；
  - `ColumnsBar` 绿标被引用列（6.1）；
  - `ColumnsBar` 列多时下拉框展示全部（6.2）。
