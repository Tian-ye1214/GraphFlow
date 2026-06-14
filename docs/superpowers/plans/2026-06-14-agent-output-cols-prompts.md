# agent 产出列替换语义 + 提示词全外置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复自动处理 agent 操作「删/只保留列」时输出列解析错误（改为替换语义），并把剩余硬编码提示词全部外置为 `app/agent/prompts/*.md`。

**Architecture:** Part A 把 `auto_process` 的 agent 操作产出列由「并入」改「替换」（后端 `columns.py` + 前端 `liveOutput` + codegen 指令改让 AI 声明完整 schema）。Part B 沿用既有 `load_prompt` 机制把 codegen/compactor/qc/goal_loop 的硬编码提示词移进同一 `prompts/` 目录，无占位符的原样加载、带占位符的 `.format()`。

**Tech Stack:** FastAPI + SQLAlchemy 2 async（不加表不加列）、pydantic-ai、pytest（asyncio auto）；React 19 + antd 6 + TS（tsc + vitest）。

---

## 约束（每个任务都遵守）

- KISS：最简实现，不预防未发生的 bug；但本批修的是**已复现** bug。
- api_key 绝不进响应/日志/提示词；codegen 仍只把**列名**进提示词、不跑真数。
- 租户隔离不变；不加表不加列；无 DB 迁移。
- 提交：两个 `-m`，第二个 `-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`。
- 严禁 `git add` `项目设计.txt`、`.idea/`、`.codegraph/`（逐文件 add）。
- Windows PowerShell 无 `&&`；多命令用 Bash 工具或 `;`。

## 文件结构

**后端改：** `app/engine/columns.py`（agent 替换）、`app/agent/codegen.py`（指令+externalize）、`app/agent/compactor.py`、`app/engine/nodes.py`、`app/agent/goal_loop.py`。
**后端新增 md：** `app/agent/prompts/` 下 `codegen_system.md`、`codegen_user.md`、`node_assist_llm_synth.md`、`node_assist_qc.md`、`compactor_system.md`、`qc_empty_anchor.md`、`goal_first_round.md`、`goal_round.md`。
**前端改：** `frontend/src/canvas/forms/NodeConfigForm.tsx`（`liveOutput` + 标签）。
**测试改：** `backend/tests/test_columns.py`（替换语义）、`backend/tests/test_agent_codegen.py`（新增「完整 schema」断言）、`backend/tests/test_agent_prompts.py`（新提示词冒烟）。

---

### Task 1: 后端 — agent 操作产出列「替换」语义（bug 核心）

**Files:**
- Modify: `backend/app/engine/columns.py:28-29`
- Test: `backend/tests/test_columns.py:47-54`（改）+ 新增两条

- [ ] **Step 1: 改测试为替换语义（先红）**

把 `backend/tests/test_columns.py` 中 `test_auto_process_agent_op_adds_declared_columns`（47-54 行）整体替换为下面三个测试：

```python
def test_auto_process_agent_op_replaces_with_declared_columns():
    """agent 操作声明=运行后的完整列集合（替换，非并入）：声明 q_english 即只剩 q_english。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["ap"]["output"] == ["q_english"]


def test_auto_process_agent_op_empty_declaration_passthrough():
    """未声明产出列（[]）→ 透传输入，不静默造列。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": []}]}}],
        [{"source": "in", "target": "ap", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ap"]["output"] == ["id", "q", "category"]


def test_workflow2_delete_all_keep_one():
    """复刻 workflow 2：llm column→q_english 后接 agent 替换为 [q_english]，下游 output 只见 q_english。"""
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_mode": "column", "output_column": "q_english"}},
         {"id": "ap", "type": "auto_process",
          "config": {"operations": [{"op": "agent", "code": "x", "output_columns": ["q_english"]}]}},
         {"id": "out", "type": "output", "config": {}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "ap", "kind": "normal"},
         {"source": "ap", "target": "out", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["id", "q", "category"]})
    assert cols["ap"]["output"] == ["q_english"]
    assert cols["out"]["input"] == ["q_english"]
```

- [ ] **Step 2: 运行，确认红**

Run: `cd backend && python -m pytest tests/test_columns.py -q`
Expected: `test_auto_process_agent_op_replaces_with_declared_columns` 与 `test_workflow2_delete_all_keep_one` FAIL（现并入语义返回 `["q","q_english"]` / 含全部列）。

- [ ] **Step 3: 改 `_apply_op` agent 分支为替换**

`backend/app/engine/columns.py` 第 28-29 行：

```python
    if kind == "agent":
        return _ordered_union([cols, op.get("output_columns") or []])
```

改为：

```python
    if kind == "agent":
        declared = op.get("output_columns") or []
        return _ordered_union([declared]) if declared else cols
```

- [ ] **Step 4: 运行，确认绿**

Run: `cd backend && python -m pytest tests/test_columns.py -q`
Expected: PASS（全部）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/columns.py backend/tests/test_columns.py
git commit -m "fix(columns): agent 操作产出列改为替换语义（删/只保留列可表达）" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 前端 — liveOutput 替换语义 + 产出列标签（bug 端到端）

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx:52`（liveOutput agent 分支）
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx:367`（标签文案）

- [ ] **Step 1: 改 liveOutput agent 分支为替换**

`NodeConfigForm.tsx` 第 52 行：

```tsx
      else if (op.op === 'agent') { cols = uniq([...cols, ...(op.output_columns ?? [])]) }
```

改为：

```tsx
      else if (op.op === 'agent') { cols = op.output_columns?.length ? uniq(op.output_columns) : cols }
```

- [ ] **Step 2: 改产出列标签文案**

`NodeConfigForm.tsx` 第 367 行：

```tsx
          <div style={{ color: '#666', fontSize: 12, marginBottom: 4 }}>产出列（本操作新增的列，AI 已填，可改）</div>
```

改为：

```tsx
          <div style={{ color: '#666', fontSize: 12, marginBottom: 4 }}>产出列（本操作运行后的全部列，AI 已填，可改）</div>
```

- [ ] **Step 3: 构建 + 单测**

Run: `cd frontend && npm run build && npx vitest run`
Expected: tsc 通过、vite build 成功、vitest 全绿。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx
git commit -m "fix(ui): agent 产出列改替换语义并改标签为'运行后全部列'" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 后端 — 外置 codegen 提示词 + 「完整 schema」改写

**Files:**
- Create: `backend/app/agent/prompts/codegen_system.md`
- Create: `backend/app/agent/prompts/codegen_user.md`
- Create: `backend/app/agent/prompts/node_assist_llm_synth.md`
- Create: `backend/app/agent/prompts/node_assist_qc.md`
- Modify: `backend/app/agent/codegen.py:1-67`
- Test: `backend/tests/test_agent_codegen.py:129-136`（加断言）

- [ ] **Step 1: 加「完整 schema」断言（先红）**

`backend/tests/test_agent_codegen.py` 的 `test_instructions_guide_grouped_dedup` 末尾加一行：

```python
    assert "完整" in INSTRUCTIONS or "全部" in INSTRUCTIONS  # 产出列=运行后完整 schema（替换语义契约）
```

Run: `cd backend && python -m pytest tests/test_agent_codegen.py::test_instructions_guide_grouped_dedup -q`
Expected: FAIL（旧 INSTRUCTIONS 写的是「仅新增」，无「完整/全部」）。

- [ ] **Step 2: 建 `codegen_system.md`**（新「完整 schema」内容，保留所有被断言的子串）

`backend/app/agent/prompts/codegen_system.md`：

```
你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
只输出一个 JSON 对象，不要任何解释或 markdown 围栏，形如：
{"code": "<Python 源码字符串>", "output_columns": ["<运行后输出行的全部列名>", ...]}
code 字段要求：
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas（可 import pandas as pd）；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。
output_columns 字段：列出 code 运行后输出行里的**全部**列名（完整 schema，不只是新增列）。删列/只保留某些列时只列最终留下的列；新增列时列出原有列加新列；无法确定则留空数组 []。

只给出上游可用列名（不含真实数据），请据指令与列名编写代码。
常见模式（按需选用、灵活组合，最后都 return 行字典列表，如 df.to_dict('records')）：
- 全局/多列复合去重：df.drop_duplicates(subset=[列...])（subset 含 'session' 即按 session 与其它列联合去重）。
- 分组内复杂处理（先按 session 分组、再对每组单独处理）：df.groupby('session', group_keys=False).apply(fn)。
- 过滤/改列：用 pandas 布尔索引或列表推导。
```

- [ ] **Step 3: 建 `codegen_user.md`**（带占位符）

`backend/app/agent/prompts/codegen_user.md`：

```
用户指令：{instruction}

上游可用列：{columns}
```

- [ ] **Step 4: 建 `node_assist_llm_synth.md`**（逐字，原样加载）

`backend/app/agent/prompts/node_assist_llm_synth.md`：

```
你为「LLM 合成」节点写配置：根据用户指令和上游可用列，写一段生成提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 指令只产出单列时：{"system_prompt":"...","user_prompt":"...","output_mode":"column","output_column":"<列名>"}。
- 指令产出多列时（让模型返回 JSON 再拆列）：{"system_prompt":"...","user_prompt":"...","output_mode":"json","output_columns":["<列名>",...]}，并让 user_prompt 要求模型只输出对应这些键的 JSON。
- user_prompt 用 {{列名}} 引用上游的可用列。
```

- [ ] **Step 5: 建 `node_assist_qc.md`**（逐字，原样加载）

`backend/app/agent/prompts/node_assist_qc.md`：

```
你为「质检」节点写判定配置：根据用户指令和上游可用列，写一段判定提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 形如 {"system_prompt": "...", "user_prompt": "..."}。
- 提示词要引导模型只输出 {"pass": true|false, "reason": "<不通过原因>"}。
- user_prompt 用 {{列名}} 引用上游的可用列。
```

- [ ] **Step 6: 改 `codegen.py` 走 load_prompt**

`backend/app/agent/codegen.py`：删掉 9-22 行的 `INSTRUCTIONS = """..."""` 整块与 47-60 行的 `NODE_ASSIST_INSTRUCTIONS = {...}` 整块，改为从 md 加载；在 import 区加入 `load_prompt`；`_user_prompt` 改用模板。

import 区（第 5 行后）加：

```python
from app.agent.prompts import load_prompt
```

`INSTRUCTIONS = """..."""`（9-22 行）整块替换为：

```python
INSTRUCTIONS = load_prompt("codegen_system.md")
```

`_user_prompt`（34-36 行）改为：

```python
def _user_prompt(instruction: str, columns: list[str]) -> str:
    cols = "、".join(columns) if columns else "（未知，按指令中提到的列名处理）"
    return load_prompt("codegen_user.md").format(instruction=instruction, columns=cols)
```

`NODE_ASSIST_INSTRUCTIONS = {...}`（47-60 行）整块替换为：

```python
NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": load_prompt("node_assist_llm_synth.md"),
    "qc": load_prompt("node_assist_qc.md"),
}
```

- [ ] **Step 7: 运行 codegen 相关测试，确认绿**

Run: `cd backend && python -m pytest tests/test_agent_codegen.py tests/test_codegen_columns.py tests/test_agent_api.py -q`
Expected: PASS（`test_instructions_guide_grouped_dedup` 含核心契约+新「完整」断言；`generate_code`/`node_config`/`KeyError`/endpoint 均绿）。

- [ ] **Step 8: 提交**

```bash
git add backend/app/agent/codegen.py backend/app/agent/prompts/codegen_system.md backend/app/agent/prompts/codegen_user.md backend/app/agent/prompts/node_assist_llm_synth.md backend/app/agent/prompts/node_assist_qc.md backend/tests/test_agent_codegen.py
git commit -m "refactor(prompts): 外置 codegen/node-assist 提示词并改产出列为完整 schema" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 后端 — 外置 compactor 摘要词 + qc 空内容锚定句

**Files:**
- Create: `backend/app/agent/prompts/compactor_system.md`
- Create: `backend/app/agent/prompts/qc_empty_anchor.md`
- Modify: `backend/app/agent/compactor.py:31-36`
- Modify: `backend/app/engine/nodes.py`（import + qc 锚定行 167-168）

- [ ] **Step 1: 建 `compactor_system.md`**（逐字一行，原样加载）

`backend/app/agent/prompts/compactor_system.md`（单行，无内部换行）：

```
你是上下文压缩器。把下面的 Agent 工作历史压缩成简洁结构化摘要，必须包含两节：【已完成】列已达成的目标/产出；【待完成】列尚未完成的任务/已知问题。只保留对继续推进有用的结论，删除寒暄与中间过程。
```

- [ ] **Step 2: 建 `qc_empty_anchor.md`**（逐字锚定句，原样加载）

`backend/app/agent/prompts/qc_empty_anchor.md`：

```
硬性规则：若待判定内容为空或缺少必要字段，必须返回 pass:false。
```

- [ ] **Step 3: 改 `compactor.py`**

`backend/app/agent/compactor.py` 的 `_default_summarize`（31-37 行）：

```python
async def _default_summarize(compactor_mc, text: str) -> str:
    from app.services import llm
    system = ("你是上下文压缩器。把下面的 Agent 工作历史压缩成简洁结构化摘要，"
              "必须包含两节：【已完成】列已达成的目标/产出；【待完成】列尚未完成的任务/已知问题。"
              "只保留对继续推进有用的结论，删除寒暄与中间过程。")
    out, _usage = await llm.chat(compactor_mc, system, text, params={}, retries=2)
    return out
```

改为：

```python
async def _default_summarize(compactor_mc, text: str) -> str:
    from app.services import llm
    from app.agent.prompts import load_prompt
    system = load_prompt("compactor_system.md")
    out, _usage = await llm.chat(compactor_mc, system, text, params={}, retries=2)
    return out
```

- [ ] **Step 4: 改 `nodes.py`（模块级常量，热路径避免逐行读盘）**

`backend/app/engine/nodes.py`：在 import 区加入

```python
from app.agent.prompts import load_prompt
```

在模块顶部常量区（紧接 import 之后）加：

```python
QC_EMPTY_ANCHOR = "\n\n" + load_prompt("qc_empty_anchor.md")
```

把 `run_qc_judge_row` 内（167-168 行）：

```python
    system = render_template(config.get("system_prompt", ""), base) + \
        "\n\n硬性规则：若待判定内容为空或缺少必要字段，必须返回 pass:false。"
```

改为：

```python
    system = render_template(config.get("system_prompt", ""), base) + QC_EMPTY_ANCHOR
```

- [ ] **Step 5: 运行相关测试，确认绿**

Run: `cd backend && python -m pytest tests/test_qc_empty.py tests/test_compactor.py tests/test_compact_seams.py -q`
Expected: PASS（`test_judge_uses_temperature_zero_and_anchor` 仍断言 `"pass:false" in system`，锚定句逐字保留）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/agent/compactor.py backend/app/engine/nodes.py backend/app/agent/prompts/compactor_system.md backend/app/agent/prompts/qc_empty_anchor.md
git commit -m "refactor(prompts): 外置 compactor 摘要词与 qc 空内容锚定句" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 后端 — 外置 goal_loop 轮次提示

**Files:**
- Create: `backend/app/agent/prompts/goal_first_round.md`
- Create: `backend/app/agent/prompts/goal_round.md`
- Modify: `backend/app/agent/goal_loop.py:1-48`

- [ ] **Step 1: 建 `goal_round.md`**（逐字模板，占位符 goal_text/run_id/metric_str/fail_str）

`backend/app/agent/prompts/goal_round.md`：

```
[目标]
{goal_text}

[上一轮运行 #{run_id} 实测：首轮质检通过率 = {metric_str}]

[真实质检失败样本抽样（含各判定模型理由）]
{fail_str}

请先**凝练通用经验**：从这些失败样本中归纳出可推广的规律（而不是针对单条样本打补丁），再据此用 gf 命令改进当前工作流的提示词/参数（必要时调整链路）。改完即结束本回合，系统会自动跑数并把新指标喂给你。仍需继续时回复末尾输出 `<!-- REDLOTUS_GOAL:CONTINUE -->`；若判断目标不可达请输出 `<!-- REDLOTUS_GOAL:DONE -->`。
```

- [ ] **Step 2: 建 `goal_first_round.md`**（逐字模板，占位符 goal_text）

`backend/app/agent/prompts/goal_first_round.md`：

```
[目标]
{goal_text}

这是目标优化模式第一轮。请先用 gf 查看当前工作流结构与质检节点，凝练你对如何达成目标的初步判断，再改进提示词/参数。改完结束回合，系统会自动跑数。回复末尾输出 `<!-- REDLOTUS_GOAL:CONTINUE -->`。
```

- [ ] **Step 3: 改 `goal_loop.py`**

`backend/app/agent/goal_loop.py`：import 区（第 2-3 行 `import json` / `from dataclasses import dataclass` 之后）加：

```python
from app.agent.prompts import load_prompt
```

`build_round_prompt`（32-41 行）整体替换为：

```python
def build_round_prompt(goal_text: str, metric, failures: list, run_id: int) -> str:
    metric_str = "（首轮尚无指标）" if metric is None else f"{metric:.1%}"
    fail_str = json.dumps(failures, ensure_ascii=False, indent=2) if failures else "（无失败样本）"
    return load_prompt("goal_round.md").format(
        goal_text=goal_text, run_id=run_id, metric_str=metric_str, fail_str=fail_str)
```

`first_round_prompt`（44-48 行）整体替换为：

```python
def first_round_prompt(goal_text: str) -> str:
    return load_prompt("goal_first_round.md").format(goal_text=goal_text)
```

- [ ] **Step 4: 运行，确认绿**

Run: `cd backend && python -m pytest tests/test_goal_loop.py -q`
Expected: PASS（`test_round_prompt_includes_metric_and_failures_and_distill` 断言 `"60" in p`、`"凝练"`、`"打补丁"`、`"q"`，逐字模板保留）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/goal_loop.py backend/app/agent/prompts/goal_first_round.md backend/app/agent/prompts/goal_round.md
git commit -m "refactor(prompts): 外置目标优化首轮/轮次提示模板" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 后端 — 新提示词冒烟测试

**Files:**
- Modify: `backend/tests/test_agent_prompts.py`（追加）

- [ ] **Step 1: 追加冒烟测试**

`backend/tests/test_agent_prompts.py` 末尾追加：

```python
def test_new_static_prompts_loadable():
    sysp = load_prompt("codegen_system.md")
    assert "def process(rows: list[dict]) -> list[dict]" in sysp and "output_columns" in sysp
    assert "完整" in sysp or "全部" in sysp                       # 替换语义契约
    assert "pass:false" in load_prompt("qc_empty_anchor.md")      # qc 锚定句
    assert "压缩器" in load_prompt("compactor_system.md")
    for name in ("node_assist_llm_synth.md", "node_assist_qc.md"):
        assert load_prompt(name).strip()


def test_new_templated_prompts_render():
    u = load_prompt("codegen_user.md").format(instruction="去重", columns="q、category")
    assert "去重" in u and "q" in u and "category" in u
    r = load_prompt("goal_round.md").format(
        goal_text="G", run_id=3, metric_str="60.0%", fail_str='[{"q": "a"}]')
    assert "凝练" in r and "60.0%" in r and "G" in r
    f = load_prompt("goal_first_round.md").format(goal_text="G")
    assert "G" in f and "REDLOTUS_GOAL:CONTINUE" in f
```

- [ ] **Step 2: 运行，确认绿**

Run: `cd backend && python -m pytest tests/test_agent_prompts.py -q`
Expected: PASS。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_agent_prompts.py
git commit -m "test(prompts): 新外置提示词加载/渲染冒烟" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾验证（全部任务后）

- [ ] 后端全量：`cd backend && python -m pytest -q` → 全绿（净 +4 用例：columns 净 +2、agent_prompts +2；agent_codegen 既有用例内 +1 断言）。
- [ ] 前端：`cd frontend && npm run build && npx vitest run` → 全绿。
- [ ] `git grep -n "你是上下文压缩器\|硬性规则：若待判定\|REDLOTUS_GOAL:CONTINUE" backend/app` 确认这些提示词只在 `prompts/*.md`、不再在 `.py`（orchestrator 的结构化拼装除外，本就保留）。
- [ ] 最终整体评审 → finishing-a-development-branch。
