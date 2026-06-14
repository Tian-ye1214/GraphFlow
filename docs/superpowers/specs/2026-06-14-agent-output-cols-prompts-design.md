# 批次七：agent 产出列「替换」语义（修 bug）＋ 提示词全外置 设计

**日期：** 2026-06-14
**分支：** `feature/agent-cols-replace-prompt-md`

承接批次六（[节点列血缘 + 质检可配参数]，master@785de86）。两项需求：

1. **修 bug**：自动处理节点的 agent 操作，指令「删除全部列，只保留 q_english」后，输出列解析不正确——现状原样不变，期望解析为 `q_english`。
2. **重构**：把硬编码提示词从代码里移出，全部放进 prompt 文件夹存为 `.md`。

贯穿约束（KISS 硬规则）：最简实现、不预防未发生的 bug（但**已复现**的 bug 必修）；api_key 全程 Fernet 加密、绝不进响应/日志/提示词；所有模型/工作流/运行/数据集引用校验 `user_id`（租户隔离）；不加表不加列；codegen 仍只把**列名**（非行值）进提示词、不跑真数。

---

## 一、实证根因（workflow 2 / user 4）

链路：`input → llm_synth_1 → auto_process_1 → qc_1 → output_1`。实测配置：

- `llm_synth_1`：`output_mode=column`，`output_column=q_english` → 输出 = 数据集列 `[id,q,category]` ∪ `[q_english]` = `[id,q,category,q_english]`。
- `auto_process_1`：单个 `agent` 操作，`instruction="删除全部列，只保留q_english"`，实测 `output_columns=[]`。

**两处叠加，缺一都修不好：**

| # | 问题 | 证据 |
|---|---|---|
| 1 | **AI 声明了空产出列** | codegen 系统指令写的是「只列**新增**的列（仅新增的，没有则 `[]`）」。「删全部、只留 q_english」没有任何"新增"列（`q_english` 早由上游产出）→ AI 据实返回 `[]` |
| 2 | **agent 操作是"并入"语义** | `_apply_op` 对 agent 做 `_ordered_union([cols, output_columns])` —— 只能**加**列、永不**减**列。即便声明 `["q_english"]`，结果也是 `输入 ∪ {q_english}` = 原样 |

两者叠加 → 节点输出恒等于输入 → 用户看到「输出列没有变动」。

**结论**：agent 操作的代码可任意增删改列，「并入」近似无法表达「删/只留」。根治 = 把 agent 操作的产出列重新定义为**代码运行后的完整列集合（替换）**，并让 AI 声明完整输出 schema。

---

## 二、Part A：agent 产出列 = 运行后完整列集合（替换语义）

### 2.1 后端 `engine/columns.py` `_apply_op`

agent 分支由「并入」改「替换」：

```python
if kind == "agent":
    declared = op.get("output_columns") or []
    return _ordered_union([declared]) if declared else cols
```

- 声明非空 → 输出 = 声明的列（去重，保持顺序）。**替换**输入列集合。
- 声明为空 → 透传输入（视作"未声明 / 不知道"，向后兼容，不静默造列）。

其余 op（rename/drop/concat/cast/filter/sample/shuffle/dedup）语义**不变**——它们是确定性结构操作，静态可推。只有 agent（任意代码）改为替换。

### 2.2 codegen 系统提示词（同时随 Part B 外置为 `codegen_system.md`）

把 `output_columns` 的说明从「仅新增的列」改为「代码运行后输出行里的**全部**列名（完整 schema）」。AI 的用户提示里已带「上游可用列」，足以算出完整输出集：

- 「删全部只留 q_english」→ 声明 `["q_english"]`。
- 「在现有列上加一列 q_en」→ 声明 `[...所有输入列, "q_en"]`。
- 「把 q 改名 query」→ 声明 `[id, query, category]`。

### 2.3 前端 `NodeConfigForm.tsx`

- `liveOutput` 的 auto_process agent 分支同步替换语义：
  ```js
  else if (op.op === 'agent') { cols = op.output_columns?.length ? uniq(op.output_columns) : cols }
  ```
- `AgentOpFields` 产出列标签文案：「产出列（本操作**新增**的列，AI 已填，可改）」→「产出列（本操作**运行后的全部列**，AI 已填，可改）」。占位「如 q_english」不变。

### 2.4 迁移

不做数据迁移（KISS）。存量节点（如 workflow 2）`output_columns=[]` 在新语义下仍透传、显示原样；用户重点「生成代码」让 AI 按新指令重新声明，或手动把产出列填 `q_english`，即解析为 `[q_english]`。

---

## 三、Part B：提示词全外置（沿用现有 `load_prompt` 机制）

### 3.1 既有模式（直接照搬，无需改 loader）

`app/agent/prompts/__init__.py` 已提供 `load_prompt(name)` 读原始文本。`system.py` 已示范两种用法：

```python
load_prompt("manager_planning_new.md")                       # 原样
load_prompt("manager_summary.md").format(user_input=..., final_summary=...)  # 模板
```

新外置一律照此：**无占位符**的原样 `load_prompt(name)`；**带占位符**的 `load_prompt(name).format(**kwargs)`。**不新增 loader 函数、不改 `__init__.py`。**

`.format()` 的大括号陷阱：含字面 JSON 大括号（如 `{"code":...}`、`{{列名}}`）的提示词**没有占位符**，一律原样加载（不调 `.format()`），故安全。带占位符的提示词静态文本里**无**字面大括号，`.format()` 安全。

### 3.2 外置清单

| 来源（现硬编码） | 新 md 文件 | 加载方式 | 占位符 |
|---|---|---|---|
| codegen.py `INSTRUCTIONS` | `codegen_system.md` | 原样 | 无（含字面 JSON 大括号）|
| codegen.py `NODE_ASSIST_INSTRUCTIONS["llm_synth"]` | `node_assist_llm_synth.md` | 原样 | 无 |
| codegen.py `NODE_ASSIST_INSTRUCTIONS["qc"]` | `node_assist_qc.md` | 原样 | 无 |
| codegen.py `_user_prompt` 模板 | `codegen_user.md` | `.format` | `instruction`、`columns` |
| compactor.py `_default_summarize` 系统词 | `compactor_system.md` | 原样 | 无 |
| nodes.py qc 空内容锚定句 | `qc_empty_anchor.md` | 原样 | 无 |
| goal_loop.py `first_round_prompt` | `goal_first_round.md` | `.format` | `goal_text` |
| goal_loop.py `build_round_prompt` | `goal_round.md` | `.format` | `goal_text`、`run_id`、`metric_str`、`fail_str` |

各源文件改动：
- **codegen.py**：`INSTRUCTIONS = load_prompt("codegen_system.md")`；`NODE_ASSIST_INSTRUCTIONS = {"llm_synth": load_prompt(...), "qc": load_prompt(...)}`；`_user_prompt` 保留拼列逻辑（`、`.join / 未知回退），模板换成 `load_prompt("codegen_user.md").format(instruction=instruction, columns=cols)`。`codegen_system.md` 内容含 Part A 的「全部列」改写。
- **compactor.py**：`_default_summarize` 内 `system = load_prompt("compactor_system.md")`。
- **nodes.py**：qc 锚定句 `+ "\n\n硬性规则：…"` 换成 `+ "\n\n" + load_prompt("qc_empty_anchor.md")`（分隔符 `\n\n` 留代码，避免文件前导空行被工具吞掉）。`qc_empty_anchor.md` 只存锚定句本身，逐字保留（含 `pass:false`）。
- **goal_loop.py**：两函数保留 `metric_str`/`fail_str` 的格式化逻辑，正文换成 `load_prompt(...).format(...)`。

### 3.3 布局与分层

- 全部进现有 `app/agent/prompts/`（已是唯一提示词文件夹，现含 9 个 md）。**不**新建顶层目录、**不**迁移现有 md。
- 跨层 import：`app/engine/nodes.py` 新增 `from app.agent.prompts import load_prompt`（唯一 engine→agent 边）。已确认无循环导入——`app/agent/__init__.py` 为空（1 行）、`app/agent/prompts/__init__.py` 只 import stdlib，且 prompts 加载链不回头 import engine。属叶子文本工具，可接受。

### 3.4 保留在代码（非提示词文案）

明确**不**外置（结构化拼装、含条件逻辑，无指令性文案）：
- `orchestrator.py` 的 Worker 提示拼装（按依赖/重试动态拼 `[用户最终目标]/[当前任务]/[前置任务结果]/[其他 Worker 进展]` 等多段）。
- `routers/model_configs.py` 的 `"ping"` 连通性测试串。

---

## 四、测试策略

**后端（pytest）：**
- `test_columns.py` 新增：
  - agent 操作声明 `["q_english"]`、输入 `[id,q,category,q_english]` → 节点 output `== ["q_english"]`（替换，非并入）。
  - agent 操作 `output_columns=[]` → 透传输入。
  - 复刻 workflow 2：llm_synth column→q_english 后接 agent 替换为 `["q_english"]`，下游 output 节点 input `== ["q_english"]`。
- 外置**逐字复制**，保证既有断言原样通过：
  - `test_qc_empty.py::test_judge_uses_temperature_zero_and_anchor` 断言 `"pass:false" in system` —— `qc_empty_anchor.md` 须含该句。
  - goal_loop 既有测试（若断言文案）须因逐字复制而通过。
  - compactor 既有测试通过。
- `generate_code` 仍返回 `{code, output_columns}`；INSTRUCTIONS 文案变更不影响结构化测试（FunctionModel 断言结构非措辞）。
- 可加一条轻测：`load_prompt("codegen_system.md")` 非空、`load_prompt("goal_round.md").format(...)` 能渲染不抛。

**前端：** `npm run build`（tsc -b && vite build）+ `npx vitest run` 保绿。`liveOutput` 为组件内部函数（无组件测试设施），靠类型与构建把关。

---

## 五、文件清单

**后端：**
- 改 `app/engine/columns.py`（`_apply_op` agent 替换语义）。
- 改 `app/agent/codegen.py`（INSTRUCTIONS/NODE_ASSIST/`_user_prompt` 改走 `load_prompt`；系统词含「全部列」改写）。
- 改 `app/agent/compactor.py`（摘要系统词外置）。
- 改 `app/engine/nodes.py`（qc 锚定句外置；新增 import）。
- 改 `app/agent/goal_loop.py`（两轮次提示外置）。
- 新增 `app/agent/prompts/`：`codegen_system.md`、`codegen_user.md`、`node_assist_llm_synth.md`、`node_assist_qc.md`、`compactor_system.md`、`qc_empty_anchor.md`、`goal_first_round.md`、`goal_round.md`。

**前端：**
- 改 `frontend/src/canvas/forms/NodeConfigForm.tsx`（`liveOutput` agent 替换语义 + 产出列标签文案）。

**测试：**
- 改 `backend/tests/test_columns.py`（新增替换语义用例）。
- 视情新增 `backend/tests/test_prompts.py`（加载/渲染冒烟）。
