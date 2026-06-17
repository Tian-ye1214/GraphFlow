# 第十九批设计：列 I/O 下拉框 + 三态删除 + 思考模式

**日期**：2026-06-17
**承接**：第十七批（http_fetch + 列可见性）、第十八批（列 I/O 可视化重做 + 助手保留上下文）

两块独立特性，同属一批、一次合并：

1. **列 I/O 重做**——列不再平铺，收进下拉框；新增「三态点击」让某列在 透传 / 喂给 LLM / 删除 间循环；删除的列下游不可见。
2. **思考模式**——全部 LLM 调用（跑数节点 + 全部 Agent 角色）默认开启思考、力度 high，可在节点级关闭/调力度。

---

## Part 1 — 列 I/O：下拉框 + 三态删除

### 背景与动机

第十八批把所有输入列平铺成可点 Tag（绿=已在 prompt 里 `{{列}}`=喂给 LLM）。列数量一多就刷屏。用户要求：

- 输入列和输出列**不要全部平铺**，用**下拉框**展示（全部列、不省略），避免列数量过多。
- 某列可标记为**删除（红色）**，删除后**下一个节点不展示**该列。

### 节点配置新增字段

所有节点 config 增加可选字段：

```
drop_columns: list[str]   # 默认 []；本节点输出里要剔除的列（下游不可见）
```

仅 `llm_synth`/`qc`/`http_fetch` 的列 bar 提供「点击设为删除」交互。`auto_process` 保留其自带 drop op，不重复（其 bar 只读展示）。引擎与血缘对**任何**带 `drop_columns` 的节点统一生效。

### 三态模型

一列在某节点的输入侧有三种状态：

| 状态 | 颜色 | 含义 | config 表现 |
|------|------|------|-------------|
| 透传 | 灰 | 不喂模型，但原样透传到输出、保存 | 既不在 prompt 的 `{{列}}` 里，也不在 `drop_columns` 里 |
| 喂给 LLM | 🟢 绿 | 拼进本节点 prompt 上下文，且透传保存 | `{{列}}` 出现在 prompt（`user_prompt`/`url` 等引用字段） |
| 删除 | 🔴 红 | 从本节点输出剔除，下游看不到 | 列名在 `drop_columns` 里（且不在 prompt 引用里） |

**点击循环**：灰 → 绿 → 红 → 灰。

- 灰 → 绿：在引用字段追加 `{{列}}`（复用第十八批 `toggleColRef` 的「插入」分支）。
- 绿 → 红：从引用字段删除全部 `{{列}}`，并把列名加入 `drop_columns`。
- 红 → 灰：从 `drop_columns` 移除列名。

绿态只对有 prompt 的节点（`llm_synth`/`qc`/`http_fetch`）有意义；这些正是开放三态交互的节点。

### 前端 `ColumnsBar` 重做（`frontend/src/canvas/forms/NodeConfigForm.tsx`）

不再平铺全部列。结构：

- **输入区**（可操作节点）：
  - 紧凑摘要常显两组 chip：🟢 喂给模型的列、🔴 已删除的列（任一组为空则不显示该组）。
  - 一个**下拉**「全部输入列 (N) ▾」（AntD `Dropdown`，菜单可滚动），列出**全部**输入列，每列一个彩色 Tag（绿/红/灰），点击循环三态。展示全、不省略。
- **输出区**（所有非 input 节点）：
  - 紧凑摘要常显 🔵 蓝色 chip（本节点新增列）。
  - 下拉「全部输出列 (M) ▾」，列出全部输出列（灰=透传、蓝=新增），只读。被删除的列不在输出列里。
- **图例**：🟢 喂给模型 / 🔴 删除(下游不可见) / 🔵 本节点新增 / 灰 透传保存。
- 不可操作节点（`auto_process`/`output`）：输入区下拉只读（无三态点击），输出区照常。

点击回调把「prompt 引用字段」与「`drop_columns`」两处改动合并进一次 `onChange`。

`liveOutput()` 末尾统一减去 `config.drop_columns`，使前端预览与引擎一致。

### 引擎落点（`backend/app/engine/runner.py`）

所有节点输出都经 `_write_unit(...)` 落库——在此设单一切口：

```python
async def _write_unit(..., out_rows, error, usage=None, qc_round=0, drop=None):
    ...
    if drop:
        out_rows = [{k: v for k, v in r.items() if k not in set(drop)} for r in out_rows]
    rec.data_json = json.dumps(out_rows, ensure_ascii=False)
    ...
```

各调用点传 `drop=cfg.get("drop_columns")`（失败行写 `[]`，drop 无副作用）：

- `_run_llm_node` 的 `work()`：`drop=cfg.get("drop_columns")`
- `_run_http_node` 的 `work()`：`drop=cfg.get("drop_columns")`
- `_run_qc_node` 最终写通过行：`drop=cfg.get("drop_columns")`
- `_run_barrier_node`：`drop=node.config.get("drop_columns")`（覆盖 auto_process/output/input；input 不会被设，无害）

### 列血缘落点（`backend/app/engine/columns.py`）

`_node_output(node, input_cols, dataset_cols)` 算完类型各自输出后，统一减去 `drop_columns`：

```python
def _node_output(node, input_cols, dataset_cols):
    out = ...  # 现有的按类型计算
    drop = set(node.config.get("drop_columns") or [])
    return [c for c in out if c not in drop] if drop else out
```

于是 `propagate_columns` 沿边传播时，下游节点的 input 自然不含被删列。

---

## Part 2 — 思考模式（全局默认开启 / high）

### 背景与动机

用户要求：思考参数作用于**全部** LLM 调用——跑数节点（`llm_synth`/`qc`）**和**全部 Agent 角色（coordinator/manager/worker/compactor）；**默认开启思考、力度 high**；可在节点级关闭或调力度。

参数有两个：是否开启思考（bool）、思考力度（`low`/`medium`/`high`/`xhigh`）。线上格式（用户给定）：

```json
{"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
```

### 传输机制：两路统一用 extra_body

- **节点路径**：`services/llm.py` 用原生 `openai.AsyncOpenAI`，`chat.completions.create(..., extra_body=...)` 直接透传。
- **Agent 路径**：pydantic-ai 1.107 的 `ModelSettings` 支持 `extra_body`（也有原生 `thinking` 字段，但其产出的 body 形状与用户给定的不同，故**不用**原生字段，改用 `extra_body` 显式发同一份 body，保证两路线上格式一致、provider 无关）。

`extra_body` 的字段会进入请求体顶层，故 `reasoning_effort` 与 `thinking` 都按用户给定形状出现。

### 共享纯函数（新建 `backend/app/thinking.py`）

```python
def thinking_extra_body(params: dict) -> dict | None:
    """根据参数算思考配置的 extra_body。默认开启、high。关闭返回 None（整段不发）。"""
    if not params.get("thinking_enabled", True):
        return None
    return {"thinking": {"type": "enabled"},
            "reasoning_effort": params.get("reasoning_effort", "high")}
```

默认值（任何地方都没显式配）→ 开启 / high。关闭只需 `thinking_enabled=False`，则整段 thinking 不发。

### 节点路径（`backend/app/services/llm.py chat()`）

`merged = {**模型默认, **节点params}` 之后：

```python
eb = thinking_extra_body(merged)
if eb is not None:
    kwargs["extra_body"] = eb
```

`qc` 节点的 `params = {"temperature": 0, **config.params, "json_mode": True}` 中的 thinking 键来自 `config.params`，经 `chat` 的 merged 生效，无需额外处理。

### Agent 路径（`backend/app/agent/factory.py create_model()`）

```python
def create_model(mc):
    params = json.loads(mc.default_params_json)
    kw = {k: params[k] for k in SETTINGS_KEYS if params.get(k) is not None}
    eb = thinking_extra_body(params)
    if eb is not None:
        kw["extra_body"] = eb
    provider = OpenAIProvider(...)
    return OpenAIChatModel(mc.model_name, provider=provider,
                           settings=ModelSettings(**kw) if kw else None)
```

模型 `default_params_json` 通常为空 → 走默认 → 全 Agent 角色开启 / high。要关某 Agent 只能改模型 `default_params_json`（无 UI，YAGNI）。

### 前端（`LlmSynthForm` / `QcForm` 的 params 区）

加两个控件，写入 `config.params`：

- `Switch「开启思考」`：`checked={params.thinking_enabled ?? true}` → `patchParams({ thinking_enabled: v })`
- `Select「思考力度」`：options `low/medium/high/xhigh`，`value={params.reasoning_effort ?? 'high'}` → `patchParams({ reasoning_effort: v })`，仅开启时启用。

Agent 角色无 UI，按全局默认走。

---

## 测试

- `thinking_extra_body`：默认 → `{thinking, reasoning_effort:"high"}`；`thinking_enabled=False` → `None`；自定义力度透传。
- `llm.chat`：默认调用带 `extra_body`（high）；`params={"thinking_enabled":False}` → 不带 `extra_body`；自定义力度正确。用假 client 捕获 `create()` 的 kwargs 断言。
- `create_model`：默认 settings 带 `extra_body`（high）；`default_params_json` 关闭 → 不带。
- 引擎 `drop_columns`：`llm_synth`/`http_fetch`/`qc`/`auto_process(barrier)` 四类节点输出落库剔除指定列；下游节点 input 不含被删列（端到端）。
- 列血缘 `propagate_columns`：带 `drop_columns` 的节点输出减列，下游随之减。
- 前端 `npm run build` 通过。

## 风险（KISS：不预防，出现再修）

- 默认全局开思考后，`qc` 的 `json_mode` + 推理在个别端点可能不兼容 → 该节点关思考即可。
- 既有 `test_llm` 的假 OpenAI client 需接受 `extra_body` kwarg（实现时核对，多半已 `**kwargs`）。
- 既有引擎/血缘测试不应回归（`drop_columns` 默认 `[]`，无 drop 时行为不变）。
