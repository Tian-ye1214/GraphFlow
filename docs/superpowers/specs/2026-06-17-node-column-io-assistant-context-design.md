# 批次十八：节点列 I/O 清晰化 + 助手保留上下文 设计

**日期：** 2026-06-17
**分支：** `feat/node-column-io-assistant`

贯穿约束（KISS 硬规则）：最简实现、不预防未发生的 bug；模型 `api_key` 全程 Fernet 加密、绝不进响应/日志/提示词；所有引用校验 `user_id`（租户隔离）；不加表不加列（沿用既有 JSON config，无 migration）。

三项需求（用户原话归纳）：
1. **RedLotus 助手要保留上下文**（bug）。实证：先「把 A 列转换为 A1」可执行；再「把 B 列转换成 B2」，结果丢失了 A→A1 的转换。
2. **输入列/输出列要实时变动且展示清楚**。现状只展示输出列、输入列被折成下拉框；无法手动控制输入列。要重做成：**点击列名 → 底色变绿（=喂给 LLM）→ 再点取消**；助手对列的改动要同步到 UI（变色）；一眼看清节点的输入/输出列。
3. **保存要全面**：输入 10 列、只处理 1 列产出，最终输出要含另外没处理的 9 列。不能因为输出节点的上游只「产出」一列就只存一列。

---

## 决策记录（已与用户敲定）

| 议题 | 决策 |
|---|---|
| 绿色用什么 | **绿底色**（不是绿点）。`<Tag color="green">` 底色变绿 |
| 绿色语义 | **绿 = 该列喂给 LLM、拼进上下文** = 该列 `{{列}}` 出现在本节点 prompt 里 |
| 输入列模型 | **绑定 prompt 引用**（不新增 config 字段）。点灰列→插 `{{列}}`→变绿；点绿列→删 `{{列}}`→变灰。助手写 prompt → 绿色自动同步 |
| 未喂给 LLM 的列 | **照常透传**到下游（不丢），所以最终保存仍含它们 |
| 输出列「手动增删」 | **方案 A**：输出区只读展示（透传列 + 新产出列分色）；产出列名在表单改；要真删列用「自动处理」节点的 `drop`（显式） |
| 列很多时 | **取消 >12 下拉框**（批次十七的 `MANY_COLS`），全部渲染成可点 chip、不省略、自动换行 |

---

## Part 1 — 助手保留上下文（修 bug #1）

### 根因
`backend/app/routers/agent.py` 的 `node_assist` 与 `codegen` 端点只接收 `instruction` + 上游列名（`gather_upstream_columns`），**从不接收节点的当前配置/代码**。`generate_node_config`/`generate_code`（`agent/codegen.py`）据此每次**从零重生**。前端 `NodeAssist` 的 `onApply = (c) => onChange({...config, ...c})` 与 `AgentOpFields` 的 `update({code, output_columns})` 用新结果覆盖旧的 → 「A→A1」后「B→B2」丢掉 A→A1。

### 改法（增量、不改协议形态）
- **后端请求体**：`NodeAssistIn` 增 `current_config: dict | None = None`；`CodegenIn` 增 `current_code: str | None = None`。
- **生成函数**：
  - `generate_node_config(model, node_type, instruction, columns, current_config)` —— 把当前配置（system_prompt/user_prompt 等）拼进给模型的用户提示。
  - `generate_code(model, instruction, columns, current_code)` —— 把当前代码拼进用户提示。
- **提示词（外置 .md）**：`prompts/node_assist_llm_synth.md`、`node_assist_qc.md`、`codegen_system.md`/`codegen_user.md` 加一段明确指令：「**下面是该节点现有配置/代码。请在其基础上按新指令增量修改，保留已有处理逻辑，不要丢失之前的转换。**」当前配置/代码为空时按从零生成（保持现状）。
- **前端**：`NodeAssist.run` 调用带 `current_config: config`；`AgentOpFields.generate` 调用带 `current_code: op.code`。

### 效果
「把 A 列转换为 A1」→ 生成含 A→A1 的 prompt/代码；再「把 B 列转换成 B2」→ 助手看到现有 prompt/代码 → 产出**同时含 A→A1 与 B→B2** 的结果。

### 边界
- 仅把当前节点配置/代码作为上下文；**不**引入跨回合的对话记忆（YAGNI）。
- 承接批次五约束：codegen/node-assist 仍只把**列名**（非行值）进提示词，不跑真实数据。
- `current_config` 里若含敏感内容（不会有 api_key——节点配置不存密钥），照常；提示词不含任何密钥。

---

## Part 2 — 列 I/O 可视化重做（`NodeConfigForm.tsx` 的 `ColumnsBar`）

### 现状
`ColumnsBar`（批次十七）：输入列 >12 时折成 antd `Select` 下拉（用户不满，藏住了列）；≤12 时 chip。绿色靠 `referencedCols`（复用 `TPL_RE`）。输出列只读蓝 Tag。

### 改法
- **删掉 `MANY_COLS` 下拉分支**。输入列一律渲染为可点 `Tag` chip，容器自动换行（列极多时垂直滚动），**不省略**。
- **输入区**（标题如「喂给本节点的列（点击切换）」）：
  - 每个上游输入列一个 chip。
  - **绿底**（`color="green"`）= 被引用（该列 `{{列}}` 在本节点 prompt 里，即喂给 LLM）。
  - 默认底色（无 color）= 未引用（不喂给 LLM，但透传）。
  - 点灰 chip → 在目标字段末尾插 `{{列}}` → 变绿；点绿 chip → **从目标字段删除全部 `{{列}}` 占位** → 变灰。
  - 目标字段：`llm_synth`/`qc` = `user_prompt`；`http_fetch` = `url`（沿用 `insertField`）。
- **输出区**（标题如「本节点产出」）：
  - **透传列**（继承自输入、本节点未改的列）：中性底色（无 color）。
  - **新产出列**（本节点新增：llm 的 `output_column`/`output_columns`、http 的 `extract` keys、auto_process 新增列）：蓝底（`color="blue"`）。
  - 只读展示（产出列名在各自表单里改 —— 方案 A）。
- **图例**：一行小字说明「绿=喂给本节点；其余列仍会透传保存」。
- 作用于 `llm_synth`/`qc`/`http_fetch`（`input` 节点无 bar；`auto_process` 沿用现有；`output` 显示透传）。

### 绿色/透传的计算（纯前端，零后端）
- `referenced = referencedCols(refText, inputCols)`（已存在，复用 `TPL_RE`）：
  - `llm_synth`/`qc`：`refText = system_prompt + user_prompt`。
  - `http_fetch`：`refText = url + body + headers 值`。
- **透传列** = `outputCols ∩ inputCols`（本节点输出里继承自输入、未改名的列）。
- **新产出列** = `outputCols` 去掉 `inputCols` 后剩下的（即本节点相对输入新增的列）。`outputCols` 沿用现有计算：`llm_synth`/`auto_process`/`http_fetch` 走 `liveOutput(type, config, inputCols)`，`qc`/`output` 走血缘 `nodeCols.output`。
- 助手改 prompt/`output_column` → `config` 变 → 上述全部即时重算 → 颜色实时同步。

---

## Part 3 — 保存全面（透传规则：显式化 + 看得见 + 测试）

### 规则（定为产品契约）
**每个节点的输出 = 全部输入列（透传）∪ 本节点新产出列；没喂给 LLM 的列照样透传。** 最终保存（output 节点）= 原始数据列 ∪ 沿途所有新增列。只有「自动处理」节点的显式 `drop` 或智能处理替换（批次七语义）才会减列。

### 现状核对
- 引擎 `run_llm_synth_row` 返回 `{**base, output_column: text}`、`run_http_fetch_row` 返回 `{**base, **extracted}` —— **已透传全部输入列**。
- 列血缘 `columns.py` `llm_synth`/`http_fetch` 输出 = `input ∪ 新列` —— 血缘也是全量。

故本批不是修引擎逻辑（已对），而是：
1. **UI 看得见**：Part 2 的输出区显式列出「透传列」，让用户一眼看到那 9 列会被保留/保存（解决「以为会丢」的认知问题）。
2. **回归测试**：补一个端到端测试 —— 输入 10 列、`llm_synth` 只 `{{其中1列}}`、输出节点产物每行含全部 10 列 + 新列。
3. **实跑确认**：实现时实际跑一遍确认没有隐藏的减列路径；若发现真减列 bug 则按 KISS 修。

---

## 数据流与边界

- **租户隔离**：助手端点已校验 `wf.user_id`/`mc.user_id`；新增的 `current_config`/`current_code` 是前端回传的本节点配置，不引入新的跨租户面。
- **不加表不加列**：仅在两个请求体加可选字段、改两个生成函数与四个提示词文件、重写一个前端组件。无 DB migration。
- **空/异常**：`current_config`/`current_code` 为空或缺省 → 退化为现状（从零生成）。输入列为空 → 输入区显示「（无／先连好上游）」（现状）。

---

## 测试策略

后端（pytest，隔离库 + monkeypatch）：
- `test_codegen`/`test_node_assist`（新或并入）：monkeypatch `create_agent`/`agent.run` 捕获传给模型的用户提示，断言 `current_code`/`current_config` 的内容出现在提示里；`current_*` 为空时提示退化为现状。
- 透传回归（并入 `test_runner.py` 或新 `test_passthrough.py`）：10 列输入 → `llm_synth` 仅引用 1 列 → output 产物每行含全部 10 列 + 产出列。
- 既有 codegen/node-assist 测试保持绿（签名加可选参数，向后兼容）。

前端：`npm run build`（tsc -b && vite build）保绿。

---

## 文件清单

**后端：**
- 改 `app/routers/agent.py`（`CodegenIn`+`current_code`、`NodeAssistIn`+`current_config`，透传给生成函数）。
- 改 `app/agent/codegen.py`（`generate_code`/`generate_node_config` 收当前状态并拼进 `_user_prompt`）。
- 改 `app/agent/prompts/node_assist_llm_synth.md`、`node_assist_qc.md`、`codegen_system.md`、`codegen_user.md`（增量修改/保留已有的指令 + 现有配置/代码占位）。

**前端：**
- 改 `src/api/types.ts`（`CodegenIn`/`NodeAssistIn` 对应请求类型如有则加可选字段）。
- 改 `src/canvas/forms/NodeConfigForm.tsx`：
  - `ColumnsBar` 重做（删下拉、全量 chip、输入绿底点击切换、输出透传/新增分色、图例）；
  - `NodeAssist.run` 带 `current_config`；`AgentOpFields.generate` 带 `current_code`。

**测试：**
- 后端 `tests/test_*`（助手上下文、透传回归）。
