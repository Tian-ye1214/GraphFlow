# GraphFlow

面向大模型训练数据合成的可视化跑数平台：画布拖拽编排「输入 → LLM 合成 → 自动处理 → 输出」管道，后台并发执行、断点续跑、失败行重跑、结果导出。

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
