# RedLotus × GraphFlow 原生 Agent 跑数平台设计

日期：2026-06-11
状态：已与用户确认（12 个澄清问题收束，设计获认可）

## 1. 背景与目标

用户自研的 RedLotus Agent 框架（pydantic-ai 驱动，Coordinator→Manager→Worker 三角色，
SKILL.md 兼容技能系统）已复制到仓库 `RedLotus/` 目录。本期把它精简移植进 GraphFlow，
做成原生 Agent 跑数平台：页面上提供对话入口，背后是 Agent，Agent 通过 `gf` CLI
操控工作流/模型/数据集（配合已有的 `.claude/skills/gf-cli` 技能现学现用）。

## 2. 范围

**包含：**
- RedLotus 核心精简移植为 `backend/app/agent/` 模块（同进程）
- Agent 会话/消息持久化 + 后台回合执行 + SSE 流式
- `gf` 状态文件按会话隔离（`GF_STATE_FILE` 环境变量）
- 前端全局助手抽屉（流式回复 + 工具步骤 + 删除确认按钮）
- 移植完成后删除 `RedLotus/` 目录（历史在原仓库）

**砍掉（不进平台）：**
TUI/REPL（textual/rich/prompt-toolkit）、QQ/微信接入（ncatbot/wechatbot）、
记忆系统（LanceDB STM、SOUL/USER.md LTM、consolidation）、RAG、browser（playwright）、
ImageGeneration、PyInstaller 打包、RedLotus 自有 config.json 模型配置层、
akshare/matplotlib 等技能附带依赖。

**保留并移植：**
三角色编排（含 Worker 并行波次与共享消息板）、**goal_mode（升格为平台特色「目标模式」，
见 §7a）**、文件工具、run_command（黑名单沿用）、todo 工具、
skills 工具（list/get/load/execute）、search_web（ddgs）、
文档解析（pymupdf/python-docx）、path_sandbox、lifecycle 精简版（回合状态跟踪）。

## 3. 总体架构

```
浏览器抽屉 ──POST /api/agent/…──► FastAPI ──► AgentTurnManager（后台 asyncio.Task）
   ▲                                              │
   │ SSE /api/events（entity=agent 增量事件）        ▼
   └──────────────────────────── pydantic-ai 三角色循环（backend/app/agent/）
                                                  │ run_command("gf …", env=GF_STATE_FILE)
                                                  ▼
                                     gf CLI ──HTTP──► 同进程 /api/*（同用户权限）
                                                  │
                                     既有 SSE 联动：画布/列表页实时上屏
```

新增运行时依赖：`pydantic-ai`、`ddgs`、`pymupdf`、`python-docx`。

## 4. 移植映射

| RedLotus 源 | 去向 |
|---|---|
| `src/agent_core/system.py`（AgentSystem、run_agent_system） | `backend/app/agent/system.py`，剥离 CLI 控制器/记忆注入 |
| `src/tools/WorkerOrchestrator.py`（并行波次、SharedMessageBoard） | `backend/app/agent/orchestrator.py` |
| `src/ModelGateway/agent_factory.py` + `model_factory.py` | `backend/app/agent/factory.py`，改读 GraphFlow ModelConfig |
| `src/tools/BasicTools.py`（文件/run_command/search_web/todo） | `backend/app/agent/tools.py`，砍 browser/ask_user/execute_file |
| `src/tools/ExtractFileContent.py` | `backend/app/agent/extract.py` |
| `src/skills/SkillsManager.py` + `SkillsTools.py` | `backend/app/agent/skills.py`，技能根目录指向仓库 `.claude/skills/` |
| `src/path_sandbox.py` | `backend/app/agent/sandbox.py`，根锚定到会话工作目录 |
| `src/prompts/{coordinator,manager,worker}_system.md` 等 | `backend/app/agent/prompts/`，平台化改写（见 §11） |
| `src/lifecycle.py`（766 行） | 精简为回合状态枚举 + 事件回调，并入 system.py |
| `src/agent_core/goal_mode.py`（CONTINUE/DONE 标记协议） | `backend/app/agent/goal.py`，解析逻辑进 TurnManager（§7a） |
| 其余（cli/、API/、memory/、RAG/、TUI、config.json 模型层） | 不移植 |

## 5. 模型配置统一

砍 RedLotus config.json 模型层。`factory.py` 从用户 ModelConfig 构造 pydantic-ai 的
OpenAI 兼容 Model（base_url + crypto.decrypt(api_key_enc) + model_name + default_params）。

会话级配置 `models_json = {"coordinator": id, "manager": id, "worker": id}`：
创建会话时传一个 model_config_id 三角色共用；前端"高级"展开可分角色覆盖。
回合开始时按 id 取 ModelConfig 并做归属校验（非本人模型 → 422）。

## 6. 数据模型

```python
class AgentSession(Base):
    id, user_id(FK, index), title(str, 默认取首条消息前 30 字),
    models_json(Text),            # {"coordinator": 1, "manager": 1, "worker": 2}
    history_json(Text, 默认"[]"), # pydantic-ai ModelMessage 序列化全量历史
    status(str, 默认 "idle"),     # idle / running
    created_at, updated_at

class AgentMessage(Base):
    id, session_id(FK, index), role(str),  # user / assistant / tool
    content_json(Text),  # user/assistant: {"text": …}
                         # tool: {"tool": "run_command", "args_brief": "gf node add llm",
                         #        "status": "ok|error", "output_brief": …, "agent_role": "worker_1"}
    created_at
```

展示走 AgentMessage（前端友好），续跑走 history_json（pydantic-ai 原生反序列化）。

## 7. 回合执行（AgentTurnManager）

仿 RunManager 的内存管理器：
- `POST /api/agent/sessions/{id}/messages {text}`：会话 status=running 时 409；
  否则落库用户消息 → status=running → `asyncio.create_task(run_turn(...))` → 立即返回
- 回合内：pydantic-ai 三角色循环；每个工具调用开始/结束、assistant 文本段
  **先落库 AgentMessage 再 publish SSE**；回合结束写回 history_json、status=idle、
  publish turn_done
- 异常：错误文本落库为 assistant 消息（前缀「执行出错:」）、status=idle，不崩进程
- 后台续跑：浏览器关闭不影响 task；重开页面 GET 历史 + SSE 接续
- 启动恢复：进程重启时 running 会话重置为 idle 并补一条「回合因服务重启中断」消息
  （Agent 回合内存态无法像 Run 那样断点续跑，如实告知）
- 并发：同会话串行；跨会话不限（LLM 端压力由各 ModelConfig 服务方承受）

API 一览：
```
POST   /api/agent/sessions                 # {model_config_id 或 models} → 会话
GET    /api/agent/sessions                 # 本人会话列表
GET    /api/agent/sessions/{id}            # 详情 + 消息列表
POST   /api/agent/sessions/{id}/messages   # 发消息触发回合
POST   /api/agent/sessions/{id}/stop       # 停止目标模式自动续轮（§7a）
DELETE /api/agent/sessions/{id}            # 删会话（含工作目录清理）
```

## 7a. 目标模式（goal_mode，平台特色）

**定位**：用户给出一个目标，Agent 自主多轮迭代直到**目标达成**——这是通用机制，
不绑定任何特定指标。Agent 根据目标自行制定验证方式，每轮循环：行动 → 对照目标
检验 → 报告进展与调整 → 续轮或宣告完成。

**跑数场景的典型例子**（动机来源，非机制边界）：跑数本质是有向有环图——质检后
回扫形成环，过程要大量调提示词、小量调链路。用户说「把这条流水线调到首轮质检
通过率 ≥90%」，Agent 进入目标循环：跑流水线 → 采样评估产出（`gf export -o` 到
会话目录 → read_file → 按目标标准评估，必要时 LLM 自评）→ 没达标就改提示词
（`gf node set llm_synth_1 prompt=…`）或微调链路 → 再跑。同样的机制也服务
「把这批数据清洗成 alpaca 格式」「给每个工作流补上输出节点」等任意多轮目标。

**协议**（沿用 RedLotus 标记机制）：Agent 在回合末尾嵌入
`<!-- REDLOTUS_GOAL:CONTINUE -->` 或 `<!-- REDLOTUS_GOAL:DONE -->`。
TurnManager 解析最终文本：CONTINUE 且连续自动轮次 < 上限
（`GRAPHFLOW_AGENT_GOAL_MAX_ROUNDS`，默认 20）→ 自动以固定续轮输入
（「继续推进目标」）提交下一回合，session 保持 running；DONE 或达上限 → 停。
用户真实消息会重置轮次计数。标记不展示给用户（前端剥离）。

**停止**：`POST …/stop` 置取消标志——当前轮跑完后不再续轮，落库一条
「目标模式已被用户停止（第 n 轮）」。前端在目标进行中显示轮次徽标与【停止】按钮。

**轮上限语义**：达上限时 Agent 收到一条系统注入说明（「已达自动续轮上限」），
正常结束回合并向用户汇报当前进展，由用户决定是否继续。

**与 P2 质检节点的衔接**：当前版 Agent 自行采样评估（export + LLM 自评）；
P2 的质检节点 + 回扫环落地后，Agent 直接读质检失败行的 `_qc_reason` 来定向改提示词，
评估成本更低、信号更准。本 spec 不包含质检节点本身（仍属 P2）。

## 8. SSE 事件协议

扩展 `events.publish(user_id, entity, entity_id, **extra)`——extra 并入 payload。
Agent 事件复用既有 `/api/events` 通道：

```json
{"entity": "agent", "id": <session_id>, "kind": "delta",      "data": "文本增量"}
{"entity": "agent", "id": <session_id>, "kind": "tool_start", "data": {"tool": …, "args_brief": …, "agent_role": …}}
{"entity": "agent", "id": <session_id>, "kind": "tool_end",   "data": {"status": "ok", "output_brief": …}}
{"entity": "agent", "id": <session_id>, "kind": "turn_done"}
```

已有页面的 useEvents 按 entity 过滤，不受影响。delta 不落库（全文在回合末落库），
断线重连最多丢打字效果不丢内容。

## 9. gf CLI 通道与多用户隔离

- `backend/app/cli.py` 改 1 处：`STATE_FILE = Path(os.environ.get("GF_STATE_FILE") or Path.home() / ".graphflow" / "cli.json")`
- 会话创建时生成 `data/agent/<session_id>/cli.json`：
  `{"server": "http://127.0.0.1:<本端口>", "cookie": make_session_cookie(user_id)}`
  ——Agent 天然以会话属主身份操作，跨用户隔离由后端既有归属校验承担
- run_command 注入 env：`GF_STATE_FILE=<会话>/cli.json`，cwd=会话工作目录；
  `gf` 命令解析为 `uv run gf`（backend 目录）或已安装的全局 gf——实现取
  `sys.executable -m app.cli` 等价调用，避免依赖外部安装
- **Worker 隔离**：spawn worker_n 时复制主 state 为 `worker_<n>_cli.json` 并注入各自 env，
  避免并行 Worker 的 `gf use` 互相覆盖
- 服务端口：从请求侧拿不可靠（回合在后台），启动时记录实际绑定端口到 app.state，
  会话创建时读取

## 10. 工具清单（移植后最终版）

| 工具 | 行为与边界 |
|---|---|
| read_file / write_file / list_directory | 路径限制在会话工作目录内（sandbox 校验），越界报错 |
| run_command | cwd=会话目录；env 注入 GF_STATE_FILE；危险命令黑名单沿用；`gf (wf|data|model) rm` 硬拦（§12）；输出截断策略沿用 RedLotus |
| search_web | ddgs，原样移植 |
| extract_file_content | PDF/docx/xlsx → 文本，路径同沙盒 |
| create_todo_list / get_todo_list / mark_task_complete / mark_task_failed | Manager 规划用，原样移植 |
| list_available_skills / get_skill_instructions / load_skill_resource / execute_skill_script | 技能根目录 = 仓库 `.claude/skills/`；execute_skill_script 的脚本路径沙盒放宽到技能目录（只读）+ 会话目录（读写） |

砍掉：browser_*、ImageGeneration、ask_user（Agent 需要澄清时直接结束回合在回复中提问）、
execute_file（与 run_command 重复）、记忆查询类。

## 11. 提示词平台化改写

`backend/app/agent/prompts/` 基于 RedLotus 原版改写，要点：
- 注入平台背景：你在 GraphFlow 数据合成平台内，用户资源（工作流/数据集/模型）通过 gf CLI 操作
- 强制技能流：操作 GraphFlow 前先 `get_skill_instructions("gf-cli")`（含键名表与 op 语法）
- 删除纪律：删除工作流/数据集/模型前必须停下，在回复末尾输出
  `[confirm_delete] gf data rm 种子集` 格式的待确认块，等用户确认
- Worker 注意事项：你有独立的 gf 状态文件，先 `gf use <目标工作流>` 再操作
- 目标模式指引（移植 goal_mode 提示词并平台化）：用户给出需要多轮推进的目标时
  进入目标循环（行动→对照目标检验→报告进展与调整→续轮），由 Agent 根据目标自行
  制定验证方式，每轮末尾输出 CONTINUE/DONE 标记；涉及数据质量评估时优先用规则
  （长度/格式/字段齐全），语义质量用 LLM 自评抽样 ≤20 条控制成本
- 砍掉原提示词中的记忆/CLI 交互相关段落

## 12. 沙盒与审批

- path_sandbox 根 = `data/agent/<session_id>/`；技能目录只读白名单
- 危险命令黑名单照搬 RedLotus（rm -rf /、eval 等）
- **删除双保险**：
  1. 提示词纪律（§11）：Agent 主动询问
  2. 硬拦截：run_command 正则匹配 `gf\s+(wf|data|model)\s+rm` 时，检查本回合用户消息
     是否含确认语义（消息以「确认」开头）；否则工具返回错误
     「删除操作需用户确认，请向用户说明并等待确认」
- 前端把 assistant 消息中的 `[confirm_delete] <命令>` 渲染为【确认删除】按钮，
  点击 = 发送新消息「确认：<命令>」触发新回合执行
- `gf node rm`（图内可重建）不在拦截范围

## 13. 前端：全局助手抽屉

- `frontend/src/agent/AgentDrawer.tsx`：挂全局 Layout，右下角浮动按钮（❦）呼出
  440px 右侧抽屉（mask=false，可边聊边看画布实时变动）
- 结构：顶栏（会话下拉 + 新建 + 模型选择）、消息流、输入框
- 消息渲染：
  - user/assistant 文本：markdown（assistant 流式打字，收 delta 追加）
  - tool 消息：单行可折叠条目 `⚙ gf node add llm ✓`（点开看 output_brief），
    标注 agent_role（如 worker_1）
  - `[confirm_delete]` 块：渲染【确认删除】按钮
- 状态：进行中显示「红莲正在工作…」且输入框禁用；目标模式进行中额外显示
  轮次徽标（「目标进行中 · 第 n 轮」）与【停止】按钮（调 stop 端点）
- assistant 文本中的 `<!-- REDLOTUS_GOAL:… -->` 标记渲染前剥离
- 订阅：useEvents 过滤 `entity==='agent' && id===当前会话`
- 模型选择：主选择器（三角色共用）+「高级」展开分角色

## 14. 测试策略

- **单测（假模型）**：pydantic-ai TestModel/FunctionModel 驱动，验证：
  工具注册与调用、沙盒越界拦截、`gf … rm` 硬拦、技能加载（gf-cli 能被发现/读取）、
  ModelConfig→pydantic-ai Model 构造（含解密）
- **会话 API 测**：建会话/发消息/409 串行/历史装载/删除会话清目录/跨用户 404
- **端到端（真实 uvicorn + FunctionModel 脚本化）**：脚本让 Coordinator 按序调
  run_command 执行 gf 命令——断言「一句话 → 图真的建出来」与 SSE 事件序列
- **目标模式测**：脚本化模型连续输出 CONTINUE n 轮后 DONE——断言自动续轮次数、
  上限截停、stop 端点中断、用户消息重置计数、标记不出现在落库展示文本
- **gf 状态隔离测**：两个会话并发各自 `gf use` 不同工作流，互不干扰

## 15. 验收标准

1. 页面抽屉里说「帮我搭一个把 q 列翻译成英文的流水线并跑起来」，Agent 通过 gf
   完成搭图+运行，画布页实时看到节点出现（假模型脚本化驱动可重现）
2. 关浏览器→回合后台继续→重开页面看到完整过程记录
3. Agent 删除数据集前必出确认按钮，未确认时 run_command 拒绝执行删除
4. 多用户/多会话并发：gf 操作互不串号（状态文件隔离 + 既有归属校验）
5. 三角色按会话配置使用对应 ModelConfig，api_key 全程不出现在任何输出/日志
6. `RedLotus/` 目录已删除，backend 不引入 playwright/lancedb/textual 等重依赖
7. 目标模式：给出任意需多轮推进的目标后 Agent 自动续轮迭代直到宣告达成
   （端到端测试以「通过率 ≥X%」为代表场景，脚本化模型重现跑→评估→改提示词→再跑），
   轮上限与【停止】按钮均生效

## 16. 分期实施顺序（单一计划内排序）

1. 移植与瘦身（agent_core/工具/skills/prompts → backend/app/agent/，假模型单测）
2. gf `GF_STATE_FILE` 支持 + 会话数据模型 + TurnManager + API
3. SSE payload 扩展 + 流式事件 + 后台续跑/重启恢复 + 目标模式自动续轮/上限/stop
4. 前端 AgentDrawer（消息流/工具条目/确认按钮/模型选择/目标轮次徽标与停止）
5. 三角色全量端到端 + 目标模式与审批硬拦专测 + 删除 RedLotus/ 目录 + README

开发约束沿用：KISS，不预防未发生的 bug；api_key 只写不读；会话隔离为硬验收项。
