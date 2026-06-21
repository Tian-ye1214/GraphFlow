---
name: gf-cli
description: Use when 需要用 GraphFlow 命令行（gf）但不确定该用哪个子命令、想要总览/核心流程、或遇到 gf 通用报错（「未登录」「未选择工作流」「无法连接服务器」、退出码 1/2/130、上传报 BOM、resolve 重名报错）；具体操作（建图/节点配置/模型/数据集/运行）各有专属技能，见路由表
---

# gf —— GraphFlow 命令行（总入口）

## Overview

`gf` 是 GraphFlow 的**瘦 HTTP 客户端**，与前端同权限同校验；每次变更经 SSE 实时反映到已打开的浏览器页面（列表页自动重拉；画布页无未保存改动则静默刷新，有改动则顶部提示「工作流已被 CLI 修改」不覆盖手动编辑）。在 `backend/` 目录下 `uv run gf …` 使用，或 `cd backend; uv tool install -e .` 装成全局命令任意目录直接 `gf`。

**核心流程**：`gf login <用户名>` → `gf use <工作流>` → 建图（节点/连线/配置）→ `gf run -f`。没 `use` 就执行节点/运行命令会报「未选择工作流」。

## 认证与状态

```powershell
uv run gf login alice                 # 默认 http://127.0.0.1:8000，--server 改地址；dev 模式用户不存在自动建
uv run gf st                          # 显示 服务器 / 用户 / 当前工作流
uv run gf logout                      # 登出并清本地 cookie + 当前工作流
```

状态文件 `~/.graphflow/cli.json`：`{server, cookie, workflow_id}`；`login` 写前两项，`use` 写第三项。换用户 `login` 会清 cookie 但保留当前工作流，必要时重新 `gf use`。状态文件路径可用环境变量 `GF_STATE_FILE` 覆盖（多环境/测试场景）。

## 路由表（按你要做的事选技能）

| 你要做的事 | 用哪个技能 | 代表命令 |
|---|---|---|
| 建/改工作流结构、连线、看图、列血缘、链路打包导入导出(.gfpkg) | **gf-workflow** | `wf add/rm/rename/restore` `use` `show` `cols` `node add/rm` `link/unlink` `wf export/import` |
| 配置节点（`node set` 键名表）、写提示词、自动处理 `op`、质检回扫 | **gf-node-prompt** | `node set` `node show` `node prompt` `op` |
| 配置/测试模型 | **gf-model** | `model ls/add/set/rm/test` |
| 上传/下载/预览/删数据集 | **gf-dataset** | `data ls/up/download/head/rm` |
| 跑工作流、看进度/状态、看结果行/日志/质检/模型对话、导出、删运行 | **gf-run** | `run` `runs` `watch` `run-show` `cancel` `rerun` `export` `rows` `logs` `model-logs` `qc` `rmrun` |
| 管理可复用提示词库（库 CRUD、版本、回滚、复制、被引用、引用到节点） | **gf-prompt** | `prompt ls/show/add/edit/rm/versions/rollback/dup` `node prompt --library` |

报「未知配置键」「不确定 node set 键名 / op 语法」→ gf-node-prompt。

RedLotus Agent 会话回看（只读，跨工作流）：`gf agent ls`（列会话） / `gf agent show <会话ID>`（看消息流）。

## 服务器没起？怎么起

```powershell
cd backend
uv run uvicorn app.main:app --port 8000      # 生产式（同时托管前端页面）
# 或热更新开发模式: uv run fastapi dev app/main.py
```

- 自定义数据目录：启动前 `$env:GRAPHFLOW_DATA_DIR = "D:\gfdata"`——必须**与启动命令在同一会话/同一次调用里设**；`Start-Process`/`Start-Job` 不继承其他调用的 `$env:`，后台起服务时把赋值和启动写在同一条命令里。
- 后台启动用 bash 语法一行最省事：`GRAPHFLOW_DATA_DIR="D:\gfdata" uv run uvicorn app.main:app --port 8000 &`（在 backend/ 下），起没起用 `curl http://127.0.0.1:8000/docs` 验证。
- gf 报「无法连接服务器」= 服务器没起，或 `gf login --server` 地址不对（看 `gf st`）。

## 跨域坑（所有子命令通用）

- **资源指代（resolve 规则）**：纯数字按 ID，否则按名字**精确匹配**；找不到报「找不到名为…的…」，重名报错并列出候选 ID（改用 ID）。适用于 `wf`/`use`/`data`/`model`/`node set dataset=/model=` 等所有引用资源处。
- **文件编码**：上传 jsonl/json/csv 即使带 BOM 也能正常上传（后端按 `utf-8-sig` 自动剥除 BOM，utf-8 失败再回退 GBK），无需特殊处理 BOM。
- **退出码**：业务错误 1（打印后端中文 detail 到 stderr），argparse 参数/用法错误 2，Ctrl+C 130。
- **不走系统代理**：gf 内部 `trust_env=False`，开了 Clash 等系统代理也不影响访问本地 127.0.0.1（否则会被代理拦成 502）。
- **前端实时联动**：CLI 每次变更经 SSE 推送给同用户已打开的浏览器页；运行详情页自身 2 秒轮询，与 CLI 无关。

## 更多

- 端到端示例脚本（建库→建图→连线→跑→导出 完整一条龙）：本目录 `scripts/build-pipeline.ps1`
- 帮助：`gf --help`、`gf <子命令> --help`（注意 `node set` 的键名表帮助里没有，见 gf-node-prompt）
