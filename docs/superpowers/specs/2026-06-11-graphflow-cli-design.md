# GraphFlow CLI 设计（gf 命令 + SSE 前端实时联动）

日期：2026-06-11
状态：已与用户确认（10 个澄清问题收束，方案 A 获认可）

## 1. 背景与目标

P1 已交付画布式前端 + FastAPI 后端。本期为 GraphFlow 增加命令行入口 `gf`：
节点 CRUD/连线、模型配置、数据集上传/查看、工作流管理、运行管理均可在终端完成；
CLI 的每次变更通过服务端推送让已打开的前端页面实时更新。

命令设计目标：简单好记、参数少、可脚本化（单条命令式，非交互 shell）。

## 2. 范围

**包含：**
- `gf` CLI（backend 项目内 console script，`uv run gf …`，可 `uv tool install -e .` 全局化）
- 后端 SSE 推送端点 `GET /api/events` 与各 mutation 端点的 publish 调用
- 前端订阅事件并联动刷新（画布页 + 四个列表页）

**不包含：**
- 运行引擎内部的状态推送（运行详情页维持现有 2 秒轮询）
- CLI 监听前端变更（单向：CLI → 前端）
- 交互式 shell / REPL 模式

## 3. 总体方案

**方案 A（采纳）：瘦 HTTP 客户端 CLI + SSE 推送。**
`gf` 是纯 HTTP 客户端（httpx + argparse），打现有 API，与前端同一条路：同样的
cookie 认证、同样的校验、同样的会话隔离，零重复业务逻辑。后端新增内存级
每用户订阅队列，mutation 提交后 publish 一行；前端用浏览器原生 `EventSource` 订阅。

否决的备选：
- **B. CLI 直写数据库**：绕过认证与图校验，SQLite 双进程写有锁风险，前端实时仍需另做。
- **C. WebSocket**：只需服务器→浏览器单向，SSE 是纯 GET 长连接、无新依赖、浏览器自动重连，更省。

## 4. 命令一览

```
gf login <用户名> [--server URL]     # 登录，默认 http://127.0.0.1:8000
gf st                                # 当前服务器 / 用户 / 当前工作流

gf wf ls                             # 工作流列表
gf wf add <名字>
gf wf rm <名|ID>
gf use <名|ID>                       # 设当前工作流，后续命令默认作用于它
gf show                              # 当前工作流图的文本视图（节点 + 连线）

gf node add <类型> [ID]              # 类型: input/llm/auto/output（也接受全名），ID 缺省自动编号
gf node set <ID> key=value ...       # 键名映射见 §6
gf node show <ID>
gf node rm <ID>
gf link <源节点> <目标节点>
gf unlink <源节点> <目标节点>

gf op add <节点ID> <操作> [参数...]   # 见 §7
gf op ls <节点ID>
gf op rm <节点ID> <序号>             # 序号为 op ls 显示的 1 起始序号

gf model ls
gf model add <名字> --url <base_url> --key <api_key> --model <model_name>
gf model set <名|ID> key=value ...   # name/model/url/key + temp/top_p/max_tokens
gf model rm <名|ID>
gf model test <名|ID>

gf data ls
gf data up <文件路径> [...]          # 多文件 multipart 上传
gf data head <名|ID> [n]             # 预览前 n 行（默认 5），按 JSON 行打印
gf data rm <名|ID>

gf run [-f]                          # 运行当前工作流；-f 跟随进度至结束
gf runs                              # 运行列表
gf watch [运行ID]                    # 跟随进度；缺省取当前工作流最近一次运行
gf cancel <运行ID>
gf rerun <运行ID>                    # 重跑失败行
gf export <运行ID> [-o 文件] [--format jsonl|csv|xlsx] [--node 节点ID]
```

**资源指代规则**：参数为纯数字按 ID，否则按名字查（GET 列表后精确匹配）；
重名时报错并列出候选 ID。节点 ID 在图内唯一，直接按 ID。

**节点类型别名**：`input→input`、`llm→llm_synth`、`auto→auto_process`、`output→output`。

## 5. CLI 实现

- `backend/app/cli.py` 单文件：argparse 子命令 + `httpx.Client`（httpx 已是 openai 的
  传递依赖，显式加入 runtime 依赖即可）；pyproject 注册 `[project.scripts] gf = "app.cli:main"`。
- 状态文件 `~/.graphflow/cli.json`：`{"server": …, "cookie": …, "workflow_id": …}`。
  `gf login` 写 server+cookie，`gf use` 写 workflow_id。
- **图编辑流程**：GET `/api/workflows/{id}` → 在 graph dict 上增删节点/边 → PUT 整图存回。
  全部复用现有端点；新节点坐标沿用前端公式 `x=80+50*n, y=80+40*n`。
- 自动编号：`gf node add llm` 生成 `llm_synth_1`、`llm_synth_2`…（与前端 nextId 同规则，
  以类型全名为前缀）；显式给 ID 则用给定值，重复时由命令直接报错。
- `node set` 不做预防性校验：键名查映射表（§6），未知键报错；值原样写入，
  合法性由后端 / 运行时校验（KISS）。
- `model set` 部分更新：先 GET 取当前值，未给的字段沿用旧值再 PUT
  （后端 PUT 要求全量字段；`key` 留空即不改 api_key，与后端语义一致）。
- `export`：流式下载保存到 `-o` 指定路径，缺省 `run{id}_{node}.{format}` 存当前目录。
- `watch`：每秒 GET 运行详情，ANSI 光标回退原地刷新各节点进度表，终态退出；
  Ctrl+C 只退出查看，不取消运行。
- 错误处理：非 2xx 打印后端 `detail`（中文），退出码 1；未登录/未 `gf use` 时给一句话提示。

## 6. node set 键名映射

| 节点 | 键 | 实际字段 |
|---|---|---|
| input | `dataset=名1,名2` | `dataset_ids`（逐个按名/ID 解析） |
| llm | `model=名\|ID` | `model_config_id` |
| llm | `system=` / `prompt=` | `system_prompt` / `user_prompt` |
| llm | `mode=column\|json` / `out=` | `output_mode` / `output_column` |
| llm | `fanout=` `conc=` `retries=` | `fanout_n` / `concurrency` / `retries` |
| llm | `temp=` `top_p=` `max_tokens=` `timeout=` `json_mode=true\|false` | `params.*` |
| output | `save_as=数据集名`（空串=关闭） | `save_as_dataset` + `dataset_name` |

数值键转 int/float，`json_mode` 转 bool，其余原样字符串。

## 7. op add 各操作语法

| 操作 | 语法 | 生成的 operation |
|---|---|---|
| 去重 | `dedup [列1,列2]`（缺省全列） | `{op:dedup, columns:[…]}` |
| 过滤 | `filter <列> <min_len\|max_len\|contains\|not_contains\|regex> <值>` | `{op:filter, column, mode, value}`（len 类值转 int） |
| 重命名 | `rename <原列> <新列>` | `{op:rename, mapping:{原:新}}` |
| 删列 | `drop <列1,列2>` | `{op:drop, columns:[…]}` |
| 拼接 | `concat <列1,列2> <目标列> [分隔符]` | `{op:concat, columns, target, sep}` |
| 转换 | `cast <列> <str\|int\|float>` | `{op:cast, column, to}` |
| 采样 | `sample <n>` | `{op:sample, n}` |
| 打乱 | `shuffle` | `{op:shuffle}` |

## 8. SSE 推送

- `backend/app/events.py`：
  - `subscribers: dict[int, set[asyncio.Queue]]`（user_id → 队列集合）
  - `publish(user_id, entity, entity_id)`：向该用户所有队列 `put_nowait({"entity": …, "id": …})`
- 新路由 `GET /api/events`（需登录）：注册队列，`text/event-stream` 流式响应，
  每事件一行 `data: {"entity":"workflow","id":3}`；连接断开（生成器被取消）即注销队列。
- **publish 触发点**（mutation 提交后各加一行）：
  - workflows：create / update / delete → `entity="workflow"`
  - models：create / update / delete → `entity="model"`
  - datasets：upload / delete → `entity="dataset"`
  - runs：create / cancel / rerun-failed → `entity="run"`
- 单进程内存实现，与现有架构（嵌入式执行引擎）一致；不做心跳、不做事件持久化，
  掉线重连后前端整体重拉即可恢复。

## 9. 前端联动

- 新增 hook `useEvents(handler)`：封装 `EventSource('/api/events')`，组件卸载关闭连接。
- **画布页**：收到 `workflow` 且 id 匹配的事件 →
  - 把当前 `fromFlow(nodes, edges)` 与上次加载/保存的图 JSON 比对（纯函数判"脏"）；
  - 干净 → 静默重新 GET 并重建画布；
  - 有未保存改动 → 顶部 Alert 提示条「工作流已被 CLI 修改，点击加载最新版本」，不丢手动编辑。
  - 画布自己保存后也会收到自身事件：此时比对必然干净，重拉幂等，无需回声抑制。
- **列表页**（工作流 / 模型 / 数据集 / 运行）：收到对应 entity 事件 → 重拉列表。
- 运行详情页不变（2 秒轮询）。

## 10. 测试策略

- **后端（pytest）**：
  - SSE：订阅后触发工作流 PUT 能收到事件；断开后队列被清理；A 用户的变更不会推给 B。
  - CLI 集成：fixture 用 uvicorn 在随机端口起真实服务器（指向 tmp 数据目录），
    跑 login → wf add → use → node add/set → link → op add → data up → run → export 全链路，
    断言退出码与服务端状态。
- **前端（vitest）**：画布脏/干净判定纯函数单测。

## 11. 验收标准

1. 仅用 `gf` 命令即可从零完成：登录 → 建模型配置 → 传数据集 → 建工作流 →
   搭节点连线 → 运行 → 跟随进度 → 导出结果。
2. 浏览器开着画布页时，CLI 修改同一工作流，1 秒内画布自动刷新（画布无未保存改动），
   或出现提示条（有未保存改动）。
3. CLI 与前端共用同一套权限：A 用户的 CLI 看不到、改不了 B 用户的任何资源。
4. api_key 仍只写不读：CLI 任何输出都不包含明文 key。
