# 节点 LLM 化 + 体验批次 设计文档

日期：2026-06-13
状态：待用户审阅
分支：`feature/agent-fixes`（base master @ f9bf6c1，未合并）
前置：`2026-06-12-agent-fixes-and-smart-process-design.md`、质检回扫已落（commits 173ed39 / 3c8ca78 / 2409dcf）

## §1 背景与需求清单

用户在质检回扫完成后提出 6 项反馈，澄清中 item 4 扩展出第 7 项：

1. `data/agent/` 目录不要按数字（会话 id）命名，跟用户名走。
2. 数据集页：上传的数据集列表要置顶，现在的上传按钮（大拖拽区）太大。
3. 画布节点变动时就要自动保存。
4. 质检模块复用 LLM——QC 只保留 LLM 判定（删规则），大部分逻辑复用 LLM 合成节点。
5. 选择数据集后展示数据列和头部几个样本；要考虑超大样本、超长列渲染。
6. 运行要支持手动中断——经澄清要**硬中断**（立即中止进行中的 LLM 请求）。
7. （从 item 4 澄清扩展）每个节点都连上 RedLotus，可由助手配置该节点的提示词和数据列输入——选定方案：**节点配置面板加助手按钮**（推广现有 codegen「生成代码」模式）。

**现状盘点（避免重复造）**：item 6 的取消按钮已存在（`RunDetailPage.tsx:64`，软取消）；item 5 的列+行预览已存在于数据集页，但输入节点的数据集选择器没有预览；item 4 当前是规则判定（`qc_split` + `_predicate`）。

不在本批次：admin 租户管理（任务 #74，仍挂起）。

全程硬规则：KISS、不预防未发生的 bug、不碰 api_keys、保持按用户隔离。

## §2 Agent 工作目录按用户名命名（item 1）

`turns.session_dir` 签名从 `session_dir(session_id)` 改为 `session_dir(username, session_id)`，返回
`(settings.data_dir / "agent" / safe(username) / str(session_id)).resolve()`。

- `safe(username)`：复用上传那套正则 `re.sub(r'[\\/:*?"<>|]', "_", username)`（清洗路径非法字符，杜绝穿越）。抽成 `turns` 内小函数或 `_safe_name`。
- 仍返回绝对路径（沿用 bug 2 的修复理由：相对路径会被 gf 子进程二次拼接）。
- 三处调用点更新：
  - `turns.py:_run_turn`：按 `user_id` 查 `User.username` 后传入。
  - `agent.py:62`（`wd = session_dir(sess.id)`）：用当前请求 `user.username`。
  - `agent.py:132`（删除会话 `shutil.rmtree`）：同上。
- 旧的数字目录（`data/agent/1` 等，均为测试号产物）留着不动，不迁移。
- **测试**：`session_dir("a/b", 7)` 产出 `.../agent/a_b/7` 且为绝对路径。

## §3 数据集页：列表置顶 + 上传改小按钮（item 2）

`DatasetsPage.tsx`：

- 删除占满顶部的 `Upload.Dragger`，换成一行紧凑工具栏：`<Upload {multiple, accept, beforeUpload→doUpload, showUploadList=false}><Button icon={<UploadOutlined/>}>上传数据集（JSONL/JSON/CSV/Excel，可多选）</Button></Upload>`。
- 数据集 `Table` 紧随工具栏置顶。预览抽屉逻辑不变。
- 无后端改动。**验收**：`npm run build` 通过；目测列表在顶部、按钮变小。

## §4 画布节点变动自动保存（item 3）

`CanvasPage.tsx` 增加防抖自动保存：

- 新 effect 监听 `[nodes, edges]`：用 `setTimeout`（~800ms，每次变动重置）防抖，回调里若 `graphFingerprint(fromFlow(nodes, edges)) !== baseline.current` 才 `PUT /api/workflows/{id}` 并更新 `baseline`。
- 指纹经 `fromFlow` 含坐标，故拖动节点也会（防抖后）持久化坐标。
- 初次 `load()` 把 nodes 从服务端塞入时，指纹等于 baseline → 不触发保存（天然避免回环）。
- CLI 冲突检测（`useEvents` → `cliChanged`）保留不变：自存后 baseline=当前本地，外部 CLI 改动指纹不同仍弹「已被 CLI 修改」。
- 手动「保存」按钮保留（显式保存/`run()` 前置保存都还在）。
- **验收**：`npm run build` 通过；逻辑简单，前端无新单测。

## §5 QC 节点改纯 LLM 判定（item 4，重写现有规则版）

### 配置形态

QC 节点配置从规则版（`condition{column,mode,value}` / `reason_field` / `reason`）改为 LLM 版：

```json
{"model_config_id": 3, "system_prompt": "...", "user_prompt": "...（用 {{列名}} 引上游列）",
 "max_rounds": 3, "concurrency": 4}
```

判定提示词需引导模型输出固定契约 `{"pass": true|false, "reason": "<不通过原因>"}`。

### 引擎

- **删** `nodes.qc_split`（规则版）。`nodes._predicate` 保留（`filter` 算子仍用）。
- **新增** `nodes.run_qc_judge_row(config, row, mc, user_sem) -> (passed: bool, reason: str, usage)`：
  - 复用 `render_template` 渲染 system/user（剥离 `_qc_reason`，与 `run_llm_synth_row` 一致）。
  - 复用 `llm.chat`，强制 json 模式（`params` 注入 `json_mode=True`）。
  - 解析 `{"pass","reason"}`：`pass` 缺失视为格式错误抛异常（fail loud）；`reason` 缺省给通用文案。
  - 这就是「复用 LLM 合成节点」的落点——共用 `render_template` + `llm.chat` 两个原语。
- **`runner._run_qc_node` 重写**（结构沿用现有有界回环）：
  1. 逐行 LLM 判定（节点级 `Semaphore(config.concurrency)`，并发），分 `passed` / `failed`；`failed` 行注入 `_qc_reason=reason`。判定进度写 `RunNodeState`（total=len(inputs)）。
  2. rescan 回扫循环不变：有 rescan 目标且有 failed 时，带 `_qc_reason` 经目标 LLM（`run_llm_synth_row`）重生成 → 再用 `run_qc_judge_row` 复判 → 累加 passed，满 `max_rounds` 仍不过则丢弃。
  3. 折叠持久化 `passed` 到 row_idx 0；token 汇总 = 判定 + 重生成两部分；`qc_round=rounds`。
  4. 全程硬中断感知（见 §7）。

### 连带改动

- `runs.py:create_run` 资源归属校验：在 `llm_synth` 之外，对 `qc` 节点也校验 `model_config_id` 归属当前用户。
- `graph.py:validate_graph`：qc 节点要求有 `model_config_id`（缺失报 `GraphError`）。rescan 边校验不变。
- `cli.py:cmd_node_set`：qc 键从 `qc_col/qc_mode/qc_value/reason_field/reason` 改为 `model`（→model_config_id）/`system_prompt`/`user_prompt`/`max_rounds`/`concurrency`。同步更新 `.claude/skills/gf-cli/SKILL.md` + `reference.md`（质检改为 LLM 判定 + 判定契约说明）。
- 前端 `NodeConfigForm.QcForm`：换成「模型选择 + 判定 system/user 提示词 + max_rounds」（形似 `LlmSynthForm`），底部保留回扫提示；接入 §8 的助手按钮。

### 测试

- 重写 `test_qc.py`：`run_qc_judge_row` 用 `FunctionModel` 返回 `{"pass":...,"reason":...}`，断言判定与原因解析；格式错误抛异常。
- 更新 `test_runner.py` 回扫两测（`test_rescan_regenerates_failed_rows` / `test_rescan_drops_persistent_failures`）：判定改为 LLM 版（FunctionModel 让首轮判 fail、重生成后判 pass）。
- 更新 `test_cli.py` qc 两测：键名改为新版。

## §6 输入节点内联数据集预览（item 5）

`NodeConfigForm.InputNodeForm`：

- 选中 `dataset_ids` 后，对每个选中数据集拉 `GET /api/datasets/{id}/rows?page=1&page_size=5`，下方内联一个紧凑 antd `Table`：列取数据集 `columns`，`ellipsis: true` + 悬浮 tooltip 看全文，`scroll={{ x: 'max-content' }}`，`size="small"`，显示前 5 行 + 数据集名。
- **超大样本**：只取头部 5 行，后端 `dataset_rows` 已分页，超大集不受影响。
- **超长列**：`ellipsis` 截断 + tooltip。
- 无后端改动（`/rows` 端点已存在并带归属校验）。**验收**：`npm run build` 通过。

## §7 运行硬中断（item 6）

`runner.py` 增加小 helper：

```python
async def _cancellable(coro, cancel_event):
    task = asyncio.create_task(coro)
    waiter = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        waiter.cancel()
        return task.result()
    task.cancel()
    raise asyncio.CancelledError
```

- `_run_llm_node.work(idx)`：把 `nodes.run_llm_synth_row(...)` 包成 `_cancellable(..., cancel_event)`；捕获 `CancelledError` 则该行不落库（保持 pending）。取消触发即把 `CancelledError` 灌进进行中的 httpx 调用，立刻中止该请求。
- `_run_qc_node` 的判定/重生成同样经 `_cancellable` 包裹。
- 节点间、起行前的 `cancel_event.is_set()` 检查不变；最终仍 `_finish('cancelled')`。
- `manager.cancel` 不变（只 `event.set()`）——不取消整个 run task，避免半写 DB。
- 前端取消按钮已存在（`RunDetailPage`），行为不变（现在变成真正立刻硬停）。
- **测试**：runner 测——`FunctionModel` 的调用 `await` 一个可控事件模拟 in-flight，提交后置 `cancel_event`，断言该行非 `done`、run 状态 `cancelled`。

## §8 每个节点接 RedLotus 助手（item 7，新）

### 后端 `POST /api/agent/node-assist`

请求：`{workflow_id, node_id, node_type, instruction, model_config_id}`（均必填，校验工作流与模型归属当前用户）。

流程：

1. 复用 `codegen.gather_sample_rows(s, workflow_id, node_id, user_id)`（带归属校验）取上游样本 → 得到可用列名。
2. 临时单 Agent（复用 `agent.factory.create_agent`，零工具、零历史、请求级生命周期，与 codegen 同构），按 `node_type` 给不同 INSTRUCTIONS：
   - `llm_synth`：产出 `{system_prompt, user_prompt, output_column}`（提示词用 `{{列名}}` 引可用列）。
   - `qc`：产出判定 `{system_prompt, user_prompt}`（引导输出 `{"pass","reason"}` 契约）。
3. 解析模型返回的 JSON 配置并返回。**不跑代码**（区别于 codegen 的 Python 试跑），只生成提示词文本。

### 前端可复用 `<NodeAssist>`

- 组件：模型选择 + 指令输入框 + 「让 RedLotus 配置」按钮（形似 `AgentOpFields`）。
- 放进 `LlmSynthForm` 与 `QcForm`；点按后用返回 JSON `patch` 填好该节点的提示词/输出列。
- input/output 无提示词 → 不接助手；auto_process 维持现有 codegen 助手。
- 「数据列输入」：LLM/QC 经 `{{列名}}` 引上游列，助手据可用列写好引用，即完成「配置数据列输入」。

### 测试

- 后端 `test_agent_*`：node-assist 端点用 `FunctionModel` 返回配置 JSON，断言解析正确 + 模型/工作流归属校验（他人资源 404/403）。

## §9 实施顺序与验收

后端先行（§2 目录 / §5 QC 引擎 / §7 硬中断 / §8 node-assist 端点），各带测试；前端随后（§3 数据集页 / §4 自动保存 / §6 内联预览 / §5 QcForm / §8 NodeAssist 组件）。

收尾：`backend` 全量 `uv run pytest -q` 绿、`frontend` `npx vitest run` + `npm run build` 绿。落 `feature/agent-fixes`，不合并（待用户确认）。
