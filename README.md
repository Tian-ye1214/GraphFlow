# GraphFlow

面向大模型训练数据合成的可视化跑数平台：画布拖拽编排「输入 → LLM 合成 → 自动处理 → 输出」管道，后台并发执行、断点续跑、失败行重跑、结果导出，外加 `gf` 命令行与内置 Agent「红莲」。

## 一键启动

Windows 双击 `start.bat`，Linux / macOS 执行 `./start.sh`：构建前端、同步依赖并以单进程启动，打开 http://127.0.0.1:8000 即可使用。生产环境请先按下文「环境变量」设置 `GRAPHFLOW_SECRET_KEY` 等变量。

## 开发（Windows / macOS / Linux）

后端（终端 1）：

```bash
cd backend
uv sync
uv run fastapi dev app/main.py        # http://127.0.0.1:8000，API 文档 /docs
```

前端（终端 2）：

```bash
cd frontend
npm install
npm run dev                            # http://127.0.0.1:5173，/api 已代理到后端
```

## 快速上手（Web 端从零到一）

以「把数据集里的中文问题翻译成英文」为例，5 分钟跑通一条流水线。

**1. 登录** — 打开 http://127.0.0.1:8000，输入任意用户名（如 `zhang`）直接进入。
左侧边栏底部显示当前用户，可随时退出换号。

**2. 配置模型** — 「模型配置」页 →「新建」：
- 名称随意（如 `通义`）；Base URL 填 OpenAI 兼容地址（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`）；
- 模型名填实际模型 ID（如 `qwen-max`）；API Key 填后加密保存，之后任何页面都不会回显。

**3. 上传数据集** — 「数据集」页 →「上传」，支持 xlsx / csv / jsonl。
上传后可点开预览，确认列名（下文用 `q` 列举例）。

**4. 搭流水线** — 「工作流」页 →「新建」→ 进入画布：
- 点「+ 输入」「+ LLM 合成」「+ 自动处理」「+ 输出」各加一个节点，拖动节点间圆点连线：输入 → LLM 合成 → 自动处理 → 输出；
- 点节点弹出配置：**输入**选数据集；**LLM 合成**选模型、User Prompt 写 `把{{q}}翻译成英文，只输出译文`、输出列名填 `q_en`；**自动处理**可加去重/过滤等操作，也可加「智能处理」操作——用自然语言描述（如 `删掉 q_en 为空的行`），选模型点「生成代码」，预览结果满意后即固化；**输出**可勾选「保存为新数据集」；
- 点「保存」。

**5. 运行** — 画布点「运行」自动跳到运行详情页：实时进度、token 统计、
失败行可单独重跑，中断后再次运行自动断点续跑。

**6. 导出** — 运行详情页选格式（xlsx / csv / jsonl）下载结果。

页面之间实时联动：CLI 或 Agent 改了工作流，已打开的画布会即时刷新
（画布有未保存改动时显示提示条而不是覆盖你的编辑）。

## 命令行工具 gf

在 `backend/` 目录内用 `uv run gf …`，或安装为全局命令：`cd backend; uv tool install -e .`。

```powershell
uv run gf login alice                 # 登录（默认 http://127.0.0.1:8000，--server 可改）
uv run gf wf add 翻译流水线
uv run gf use 翻译流水线              # 设当前工作流，后续命令默认作用于它
uv run gf node add input
uv run gf node set input_1 dataset=种子集
uv run gf node add llm
uv run gf node set llm_synth_1 model=通义 "prompt=把{{q}}翻译成英文" out=answer
uv run gf node add output
uv run gf link input_1 llm_synth_1
uv run gf link llm_synth_1 output_1
uv run gf run -f                      # 运行并跟随进度
uv run gf export 1 --format jsonl
```

`gf --help` 与 `gf <子命令> --help` 查看全部命令。浏览器中已打开的页面会通过
SSE 推送实时反映 CLI 的修改；画布上有未保存改动时不会被覆盖，而是显示提示条。
完整命令与键名表见 `.claude/skills/gf-cli/`。

## Agent 助手（红莲）

页面右下角 ❦ 呼出对话抽屉：选模型配置 → 新建会话 → 直接说需求
（如「帮我搭一个把 q 列翻译成英文的流水线并跑起来」）。Agent 通过 gf CLI
操作你的资源，画布实时联动；回合在后台执行，关掉页面也会继续。

- **目标模式**：给一个需要多轮推进的目标（如「首轮质检通过率调到 90%」），
  Agent 自动循环「行动→检验→调整→续轮」直到宣告达成；上限
  `GRAPHFLOW_AGENT_GOAL_MAX_ROUNDS`（默认 20），进行中可随时点【停止】。
- **删除保护**：删工作流/数据集/模型前 Agent 必须征求确认（界面出现确认按钮），
  未确认的删除命令会被硬拦截。
- 会话工作目录在 `backend/data/agent/<会话id>/`，每个会话/Worker 持有独立 gf 状态。
- **智能处理操作**：画布「自动处理」节点里可添加「智能处理」操作——写一句自然语言，
  Agent 看着上游样本数据生成 Python 处理代码并试跑给你预览，确认后固化进节点，
  运行时在独立子进程里执行（120 秒超时保护）。
- Agent 面板停靠在页面底部（像终端一样），右下角 ❦ 呼出。

## 测试

```bash
cd backend && uv run pytest            # 后端
cd frontend && npm test                # 前端
```

## 生产部署（Linux，单进程）

```bash
cd frontend && npm install && npm run build    # 产物输出到 backend/static
cd ../backend && uv sync
export GRAPHFLOW_SECRET_KEY=<随机长字符串>      # 必须修改，用于会话签名与 api_key 加密
export GRAPHFLOW_DATA_DIR=/var/lib/graphflow   # 数据目录（SQLite/上传/导出）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://<host>:8000` 即可使用（开发模式登录：输入用户名直接进入）。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `GRAPHFLOW_DATA_DIR` | `data` | 数据落盘目录 |
| `GRAPHFLOW_SECRET_KEY` | `dev-secret-change-me` | 会话签名 + api_key 加密密钥，生产必改 |
| `GRAPHFLOW_AGENT_GOAL_MAX_ROUNDS` | `20` | Agent 目标模式自动续轮上限 |
