# 批次六：节点列血缘 + 质检可配参数 设计

**日期：** 2026-06-14
**分支：** `feature/column-lineage`

把用户两项需求收敛为一份设计。贯穿约束（KISS 硬规则）：最简实现、不预防未发生的 bug；api_key 全程 Fernet 加密、绝不进响应/日志/提示词；所有模型/工作流/运行/数据集引用校验 `user_id`（租户隔离）；不加表不加列（沿用既有 JSON config 结构）。

两项需求：
1. **质检节点可配 temperature / top_p 等参数**（与 LLM 节点一致）。
2. **每个节点的输入/输出列要明确**（产品核心）。实证 `workflow 2`：自动处理节点默认处理 `q`，实际应处理 `q_en`。

---

## 实证根因（workflow 2，user 4）

数据集 1 列 = `["id","q","category"]`。链路 `input_1 → llm_synth_1 → auto_process_1 → qc_1 → output_1`（外加 `qc_1 →(rescan) llm_synth_1`）。

| 节点 | 用户以为的输出 | 实际输出 | 问题 |
|---|---|---|---|
| `input_1` | `id,q,category` | `id,q,category` | ✓ |
| `llm_synth_1` | `q_en,category_en` | `id,q,category,**a**` | 节点是 `output_mode:"column"` + `output_column:"a"`，模型返回的 `{q_en,category_en}` JSON 被整段塞进 `a` 列；`q_en` 根本不是列 |
| `auto_process_1` | 处理 `q_en` | `df['q_english']=df['q']` | AI 写代码时只能看到 `id/q/category/a`（无 `q_en`），默认拿了 `q` |
| `qc_1` | 判 `{{q_en}}` | 渲染成空串 | `q_en` 不存在 → 内容为空 → 批次五兜底全判不通过 |

**结论：** 不是孤立 bug，而是整条链路的**列血缘不可见**。`gather_upstream_columns` 既不沿 `llm_synth` 传播新增列，也不知道 json 模式 / AI 代码会产出什么列；没有任何地方提示 `q_en` 丢了。根治方式 = 静态列血缘 + 不可推断处自声明 + 把列明确暴露到面板并对缺列告警。

---

## ① 质检可配参数（默认 0、可覆盖）

**现状：** `QcForm` 只暴露判定模型 / pass_k / 提示词 / 回扫轮数，无参数块。批次五在 `run_qc_judge_row` 把判定写死 `params = {**config.get("params", {}), "json_mode": True, "temperature": 0}`，强制温度 0。

**改动（前端 `QcForm`）：** 加与 `LlmSynthForm` 同款参数 `Space` 块——temperature / top_p / max_tokens / 超时(秒)。**不放** json_mode 开关（质检必须解析 JSON，强制开）。

**改动（后端 `engine/nodes.py` `run_qc_judge_row`）：** 改为
```python
params = {"temperature": 0, **config.get("params", {}), "json_mode": True}
```
默认温度 0、用户在 `config.params` 里设的值（含 temperature/top_p/max_tokens）覆盖默认、json_mode 末位恒为真。空样本兜底（`not any(str(v).strip() ...)` 直接判不通过不调 judge）与 system 锚定句（「内容为空必 pass:false」）**保留不动**。

---

## ② 节点列血缘（产品核心）

### 2.1 架构主线：静态列血缘模块

新增纯函数模块 `backend/app/engine/columns.py`。核心：

```python
def propagate_columns(graph: Graph, dataset_cols: dict[int, list[str]]) -> dict[str, dict]:
    """按 topo_order（只走 normal 边）推算每个节点的输入/输出列。
    返回 {node_id: {"input": [...], "output": [...]}}。纯函数，不碰 DB。"""
```

- 顺序：`topo_order(g)`（仅 normal 边；rescan 反馈边不影响 schema）。上游先算，保证下游能取到上游 output。
- 某节点 **input** = 其所有 normal 上游 output 的**有序并集**（首次出现顺序；去重）。
- 某节点 **output** = 按下表对 input 变换。

另配一个 DB 辅助（在 router 里，不在纯函数模块）：解析工作流图中所有 `input` 节点引用的 `dataset_ids`，按 `user_id` 校验归属后取 `columns_json`，组装 `dataset_cols` 传入 `propagate_columns`。

### 2.2 列传播语义

| 节点 | output = |
|---|---|
| `input` | 该节点 `config.dataset_ids` 各数据集 `columns_json` 的有序并集（非本人数据集跳过） |
| `llm_synth`（column 模式，`output_mode` 缺省即 column） | input ∪ `{config.output_column or "output"}` |
| `llm_synth`（json 模式） | input ∪ `config.output_columns`（声明，见 2.3） |
| `auto_process` | 折叠 `config.operations` 每个 op（见下） |
| `qc` | input（透传） |
| `output` | input（透传） |

`auto_process` 单 op 对列集合的变换（在已含 input 的有序列表上逐个应用）：

| op | 变换 |
|---|---|
| `rename` | 按 `op.mapping` 改名（保持位置） |
| `drop` | 移除 `op.columns` |
| `concat` | 追加 `op.target`（若不存在） |
| `cast` / `filter` / `sample` / `shuffle` / `dedup` | 不变 |
| `agent` | input ∪ `op.output_columns`（声明，见 2.3；缺省视作透传 input） |

*说明：* `agent` 代码理论上可删列，但静态不可知；按「加列」近似（声明=新增列并入），覆盖「新增 q_english」这类主用例即可，KISS。

### 2.3 不可静态推断的两处 → 节点自声明（AI 自动填，可改）

- **llm_synth json 模式：** 新增 config 字段 `output_columns: list[str]`（如 `["q_en","category_en"]`）。
  - 前端 `LlmSynthForm`：json 模式下显示「JSON 输出列（解析后拆出的列名）」tags 输入。
  - `node-assist` 助手：指令产出时一并给 `output_mode` + 对应输出字段（column 模式给 `output_column`，json 模式给 `output_columns`），由 AI 按指令决定模式。
- **auto_process 的 AI 代码：** `generate_code` 改为返回 `{"code": str, "output_columns": list[str]}`（AI 写代码时声明新增哪些列），存进该 op 的 `output_columns`。前端 `AgentOpFields` 显示可编辑「产出列」tags（默认用 AI 给的）。

*非目标：* 不在运行期强制校验模型实际产出的键是否等于声明。声明 = 设计期契约，仅用于静态传播/展示/告警；运行期 `llm_synth` json 仍按模型实际返回的键 `{**base, **parsed}`。不加运行期拦截（KISS）。

### 2.4 把列「明确」地暴露出来

- **新端点** `GET /api/workflows/{id}/columns` → `{node_id: {input:[...], output:[...]}}`。服务端解析保存的图、组装 `dataset_cols`（user_id 校验）、跑一次 `propagate_columns`。工作流不存在或非本人 → 404。
- **配置面板**每个节点顶部一行只读展示：`输入列：id、q、category ｜ 输出列：id、q、category、a`。
  - **输入列**取自端点（基于已保存的上游图）。
  - **当前编辑节点的输出列**用「输入列 + 该节点自己的 `output_column`/`output_columns`/agent `output_columns`」**客户端实时重算**（仅单节点拼接，非全图传播），改名/换模式即时更新。
- **缺列软提示：** 扫描当前节点 `system_prompt`/`user_prompt` 里的 `{{列}}`（正则 `\{\{\s*([^{}]+?)\s*\}\}`），凡引用了不在输入列中的列 → 字段下方黄字「`{{q_en}}` 上游未产出」。**只提示、不阻断保存/运行。** 作用于 `llm_synth` 与 `qc` 两类带提示词节点。workflow 2 的 qc 会立刻飘 `{{q_en}}`/`{{category_en}}`。
- **可点列名插入（轻量 polish）：** 输入列以可点 chip 列在 prompt 框旁，点一下把 `{{该列}}` 插入到对应 prompt 文本末尾。
- **codegen 改用静态传播：** `gather_upstream_columns` 重写为「解析图 → 组装 dataset_cols → propagate → 取该节点 input」。删掉旧的「采最近一次运行行键」与「仅数据集列」两套逻辑及 `_upstream_run_rows`/`_columns_of`/`_upstream_dataset_columns`/`SAMPLE_N` 与 `Run`/`RunRow`/`WorkflowVersion`/`Dataset` 相关 import 中不再需要的部分。从此 AI 写处理代码时能看到 `q_en`，不再默认 `q`。返回 `(columns, source)`，`source` ∈ `{"computed","none"}`（前端只用 `none` 判定提示「未检测到上游列」）。

---

## 数据流与边界

- **租户隔离：** `/columns` 端点与 `gather_upstream_columns` 组装 dataset_cols 时，`ds.user_id != user.id` 的数据集一律跳过（其列不出现）。工作流本身已按 `wf.user_id == user.id` 校验。
- **api_key：** 本批不接触模型调用密钥路径；codegen/node-assist 仍只把列名（非行值）进提示词，承接批次五「禁真实跑数」约束。
- **不加表不加列：** 仅在既有节点 JSON config 内新增可选键（`output_columns`）、新增一个只读查询端点、改两个函数。无 DB migration。
- **空/异常：** 图里引用了已删除的数据集 → 该数据集跳过，其列缺失（属用户数据问题，自然反映为缺列告警，不静默兜造）。节点 id 不在图中 → 返回空列（沿用既有 `gather_upstream_columns` 的处理姿态）。

---

## 测试策略

- 后端（pytest）：
  - `test_columns.py`（新）：直接复刻 workflow 2 场景——
    - column 模式 `llm_synth` 输出含 `a` 而非 `q_en`；
    - 把 `llm_synth` 改 json 模式并声明 `output_columns=["q_en","category_en"]` 后，下游 `auto_process`/`qc` 的 input 含 `q_en`；
    - `auto_process` agent op 声明 `output_columns=["q_english"]` 后并入下游；
    - `rename`/`drop`/`concat` 对列集合的变换；
    - 有序并集去重；rescan 边不影响传播。
  - 更新 codegen 测试：`generate_code` 返回 `{code, output_columns}`（JSON）、`gather_upstream_columns` 走静态传播（无 run 也能拿到 `q_en`）。
  - `test_qc_params`（新或并入既有）：`config.params` 的 temperature/top_p 透传、未设时默认 temperature 0、json_mode 恒为真、空样本兜底与锚定句仍在。
  - `/api/workflows/{id}/columns` 端点：返回各节点 input/output、租户隔离（非本人工作流 404、非本人数据集列不出现）。
- 前端：`npm run build`（tsc -b && vite build）+ `npx vitest run` 保绿（无组件测试设施，靠类型与既有单测）。

## 文件清单

**后端：**
- 新增 `app/engine/columns.py`（`propagate_columns` 纯函数）。
- 改 `app/agent/codegen.py`（`gather_upstream_columns` 走静态传播、`generate_code` 返回 JSON、`generate_node_config`/node-assist 指令支持声明输出列；清理不再用的 run/dataset 逻辑）。
- 改 `app/routers/agent.py`（codegen 端点返回 `output_columns`；node-assist 透传带 `output_columns`/`output_mode` 的 config）。
- 改 `app/routers/workflows.py`（新增 `GET /{id}/columns`；组装 dataset_cols 的 DB 辅助）。
- 改 `app/engine/nodes.py`（`run_qc_judge_row` 温度默认可覆盖）。

**前端：**
- 改 `api/types.ts`（`CodegenOut` 加 `output_columns`；新增 `ColumnsMap` 类型）。
- 改 `canvas/forms/NodeConfigForm.tsx`：
  - 取列 hook（拉 `/workflows/{id}/columns`）；
  - 每节点顶部输入/输出列展示 + 可点 chip 插入；
  - `LlmSynthForm` json 模式 `output_columns` tags；
  - `AgentOpFields` 可编辑「产出列」tags；
  - `QcForm` 参数块（temperature/top_p/max_tokens/超时）；
  - `llm_synth`/`qc` 缺列软提示。
