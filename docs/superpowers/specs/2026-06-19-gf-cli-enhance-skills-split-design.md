# gf CLI 增强 + cli/ 包重构 + 技能按资源拆分 设计（spec 1）

> 日期：2026-06-19　分支：`gf-cli-enhance-skills-split`

## 目标

把 GraphFlow 的命令行 `gf` 做全面：补齐覆盖后端 API 的命令（看运行结果/日志/质检、列血缘、整图导入导出、数据集下载、节点提示词从文件/编辑器写入），把单文件 `app/cli.py`（640 行）重构成 `app/cli/` 包，并把单个 `gf-cli` 技能按资源拆成「总入口 + 5 个资源技能」。

## 范围与拆分（重要）

本设计是**第一个 spec**，与下面两件事解耦：

- **本 spec（1）做**：CLI 命令补齐 + `cli/` 包重构 + 技能按资源拆 + **唯一一个后端新端点（数据集导出）**。节点提示词支持「从文件 / 编辑器 / stdin 写入」。
- **后续 spec（2）做（另开 brainstorm，本 spec 不碰）**：可复用**提示词库**——后端数据模型 + API、前端管理页（markdown 渲染）、`gf prompt` CLI 子命令、对应技能。
- **不纳入任何 spec（本次明确排除）**：RedLotus agent（codegen/节点助手/目标模式）、admin（用户管理/act-as）、数据集行级增删改、对现有命令的重命名。

## 全局约束（每个任务都隐含遵守）

- **入口不变**：`pyproject.toml` 的 `gf = "app.cli:main"` 必须继续有效——重构后 `app/cli/__init__.py` 必须再导出 `main`。
- **测试兼容**：现有 `backend/tests/test_cli.py` 用 `import app.cli as cli` + `cli.main([...])` + `monkeypatch.setattr(cli, "STATE_FILE", ...)`。重构后 `cli.STATE_FILE` 与 `cli.main` 必须仍可从 `app.cli` 顶层访问且 monkeypatch 能命中实际读取处（状态读写不要分散到子模块各自的模块全局，否则 patch 打不中）。
- **向后兼容**：现有所有命令拼写不变（`gf wf/use/show/node/link/unlink/op/model/data/run/runs/watch/cancel/rerun/export`）。
- **租户隔离**：新端点必须用与现有一致的 `user_id` 校验（数据集 `ds.user_id == user.id`），越权返回 404/403。
- **密钥安全**：任何输出/日志/响应绝不出现 api_key / Authorization 明文（沿用 `gf model ls` 只显示「已配置/未配置」）。
- **KISS**：最简实现，不为不发生的情况写防御代码。
- **中文输出**：CLI 面向用户的提示与报错保持中文，与现状一致。
- **不走系统代理**：新建的 httpx 调用沿用 `trust_env=False`（避免 Clash 等系统代理拦本地请求成 502）。

## 一、CLI 代码结构：`app/cli.py` → `app/cli/` 包

```
app/cli/
  __init__.py     # main(argv) 组装 argparse + 异常处理；STATE_FILE/load_state/save_state 也定义在此（供 monkeypatch 命中）；再导出供 `app.cli:main` 入口
  client.py       # Cli 类、die、resolve、convert、parse_kv、各常量表(NODE_TYPES/LLM_CONFIG_KEYS/...)
  commands/
    __init__.py
    auth.py       # login / logout / st
    workflow.py   # wf ls/add/rm/rename/restore、use、show、cols、wf dump/load、node add/rm、link/unlink
    node.py       # node set(全键)、node show、node prompt、op add/ls/rm
    model.py      # model ls/add/set/rm/test
    dataset.py    # data ls/up/download/head/rm
    run.py        # run/runs/watch/cancel/rerun/export、rows/logs/qc、rmrun
  __main__.py     # 两行 shim：from app.cli import main; main()（支持 python -m app.cli）
```

- 每个 `commands/*.py` 暴露 `register(sub)`，把自己的子命令挂到顶层 `sub`（保持现有扁平命令结构，不引入新层级）。
- `app/cli/__init__.py` 的 `main(argv)` 依次调用各模块的 `register(sub)`，解析后 `args.func(args)`；异常处理（ConnectError/ValueError/KeyboardInterrupt）与现状一致。`pyproject.toml` 的 `gf = "app.cli:main"` 因此天然有效。
- `STATE_FILE`/`load_state`/`save_state` 定义在 `__init__.py`；`client.Cli` 通过 `from app import cli` 后读 `cli.STATE_FILE`（或调 `cli.load_state()`）间接引用，确保 `monkeypatch.setattr(cli, "STATE_FILE", ...)` 在实际读取处生效。

> 重构是纯结构调整：先在不加新命令的前提下让 `test_cli.py` 全绿，再逐资源加新命令。

## 二、命令补齐（新增/改动，按资源）

### auth（技能 gf-cli 总入口）
| 命令 | 行为 | 端点 |
|---|---|---|
| `gf logout` | 登出：调用后端登出并清空本地状态里的 cookie 与 workflow_id（保留 server，方便重登） | POST /auth/logout |

（`login` / `st` 已有，不变。）

### workflow（技能 gf-workflow）
| 命令 | 行为 | 端点 |
|---|---|---|
| `gf wf rename <ref> <新名>` | 改工作流名 | PUT /workflows/{id} `{name}`（后端已支持） |
| `gf cols [节点]` | 打印当前工作流各节点输入/输出列（给一个节点则只看它） | GET /workflows/{id}/columns |
| `gf wf dump [-o 文件]` | 把当前工作流的 graph 导出为 JSON 文件（缺省 `<工作流名>.json`） | GET /workflows/{id} |
| `gf wf load <文件>` | 读 JSON 文件覆盖当前工作流 graph | PUT /workflows/{id} `{graph}` |

### node（技能 gf-node-prompt）
**`gf node prompt <id> (--system|--user) (--file P | --edit | -)`**：把长 markdown 提示词写进节点。
- `--system` / `--user` 二选一，指定写哪个字段（system_prompt / user_prompt）。
- 来源三选一：`--file P`（读文件全文）、`--edit`（打开 `$EDITOR`，缺省 vi/notepad；存盘后写入）、`-`（读 stdin，支持管道/heredoc）。
- 写入后 PUT graph 落库；内联 `gf node set <id> prompt=...` 仍保留（短提示词方便）。

**`gf node set` 补键**（值转换沿用 convert）：
| 新键 | 实际字段 | 说明 |
|---|---|---|
| `drop=列1,列2` | drop_columns | 节点级删列，下游不可见（对应前端红态） |
| `status_col=名` | status_column | qc 节点的状态列名（默认 qc_status） |
| `feedback_col=名` | feedback_column | qc 节点的反馈列名（默认 qc_feedback） |
| `outs=q_en,cat_en` | output_columns | llm 节点 mode=json 时拆出的多列名 |
| `think=on\|off` | params.thinking_enabled | 思考开关 |
| `effort=low\|medium\|high\|xhigh\|max` | params.reasoning_effort | 思考力度 |
| `headers=K1:V1,K2:V2` | headers | http 节点请求头（逗号分隔多对，冒号分键值） |

### dataset（技能 gf-dataset）
| 命令 | 行为 | 端点 |
|---|---|---|
| `gf data download <ref> [-o 文件] [--format jsonl\|csv\|xlsx]` | 下载整个数据集到文件（缺省 `<数据集名>.<格式>`，默认 jsonl） | **新端点** GET /datasets/{id}/export |

### run（技能 gf-run）
| 命令 | 行为 | 端点 |
|---|---|---|
| `gf rows <run_id> [--node N] [--failed] [--page P]` | 终端分页看某运行某节点的结果行；`--failed` 看失败行；缺省 node = 第一个输出节点 | GET /runs/{id}/rows |
| `gf logs <run_id> [--model]` | 看运行日志时间线；`--model` 改看模型对话记录 | GET /runs/{id}/logs、/runs/{id}/model-logs |
| `gf qc <run_id> [--download [-o 文件]]` | 打印质检指标(首轮通过率)+失败样本；`--download` 落 jsonl（缺省 `run<ID>_qc_failures.jsonl`） | GET /runs/{id}/qc-metrics、/qc-failures、/qc-failures.jsonl |
| `gf rmrun <run_id>` / `gf rmrun --all` | 删单次运行 / 清空全部运行 | DELETE /runs/{id}、DELETE /runs |

> 删除命名用扁平 `gf rmrun`（不做 `gf run rm`，因 `gf run`=执行、`gf runs`=列表已占用），与现有扁平 watch/cancel/rerun 风格一致。

## 三、后端改动（spec 1 唯一）

新增 `GET /api/datasets/{ds_id}/export?format=jsonl|csv|xlsx`（默认 jsonl）：

```python
@router.get("/{ds_id}/export")
async def export_dataset(ds_id: int, format: Literal["jsonl","csv","xlsx"] = "jsonl",
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await session.get(Dataset, ds_id)
    if ds is None or ds.user_id != user.id:        # 租户隔离
        raise HTTPException(status_code=404, detail="数据集不存在")
    recs = (await session.execute(select(DatasetRow).where(
        DatasetRow.dataset_id == ds_id).order_by(DatasetRow.idx))).scalars().all()
    rows = [json.loads(r.data_json) for r in recs]
    filename = f"{ds.name}.{format}"
    path = await asyncio.to_thread(export_rows, rows, format,
                                   settings.data_dir / "exports" / filename)
    return FileResponse(path, filename=filename)
```

复用 `app.services.export.export_rows(rows, fmt, path)`（与运行导出同序列化）。配套测试：导出 jsonl/csv/xlsx 三格式行数/内容正确 + 越权（他人数据集）返回 404。

## 四、技能重组（按资源 + 总入口）

`.claude/skills/` 下：

- **gf-cli**（瘦总入口）：总览、安装（`uv tool install -e .`）、`login`/`logout`/`st`、**路由表**（哪类操作看哪个技能）、跨域坑（jsonl BOM、resolve 重名规则、退出码、前端 SSE 实时联动、服务器没起怎么办、不走系统代理）。
- **gf-workflow**：图拓扑——`wf ls/add/rm/rename/restore`、`use`、`show`、`cols`、`wf dump/load`、`node add/rm`、`link/unlink`。
- **gf-node-prompt**：节点配置与提示词——`node set`（**全部键的逐键作用说明表**）、`node show`、`node prompt`（文件/编辑器/stdin）、`op`（8 种操作语法）、qc 回扫（rescan 边 + 判定 JSON 契约 `{"status":...}`）。
- **gf-model**：`model ls/add/set/rm/test`（永不显示明文 key）。
- **gf-dataset**：`data ls/up/download/head/rm`（含 BOM 坑）。
- **gf-run**：`run/runs/watch/cancel/rerun/export`、`rows/logs/qc`、`rmrun`。

要求：
- 每个技能独立 `SKILL.md`，frontmatter 的 `description` 写清触发场景（用户报「未知配置键」「未选择工作流」时能命中正确技能），互不臊肿。
- **逐键写清作用**（用户明确要求）：`gf-node-prompt` 的 `node set` 键名表必须每个键一行写明「设什么 / 别名 / 实际字段 / 取值示例」，包括本次新增的 drop/status_col/feedback_col/outs/think/effort/headers，并标注哪些键属于哪种节点类型。
- 旧 `gf-cli/reference.md` 内容按资源分配进对应技能；示例脚本 `scripts/build-pipeline.ps1` 拆/链接到相关技能（端到端示例放 gf-cli 或 gf-run）。
- 质检判定契约更新为 `{"status":"pass"|"failed"|...,"reason":...}`（批 21 已改），技能里不要再写旧的 `{"pass":true|false}`。

## 五、测试与验证

- **CLI 命令**：沿用 `backend/tests/test_cli.py` 的真实 uvicorn + `cli.main([...])` + `capsys` 模式，每条新命令至少一条测试（happy path + 关键报错）。重构阶段先让现有 CLI 测试全绿。
- **后端新端点**：用 `auth_client`/`session_factory` 测三格式导出 + 越权 404。
- **回归**：后端 `python -m pytest -q -p no:cacheprovider` 全绿；前端不涉及，无需跑。
- 技能本身无自动化测试；交付前人工核对每个 SKILL.md 的命令拼写与键名表与实现一致。

## 六、错误处理

沿用现状：HTTP 4xx/5xx 打印后端中文 detail 到 stderr、退出码 1；argparse 用法错误退出码 2；`httpx.ConnectError` → 「无法连接服务器」；`Ctrl+C` → 130。新命令的参数校验（如 `node prompt` 必须给 `--system`/`--user` 之一与来源之一）走 argparse 互斥组，缺失即用法错误退出码 2。

## 七、明确不做（YAGNI）

提示词库与 `gf prompt`（→ spec 2）、前端任何改动、agent、admin、数据集行级增删改、命令重命名、多服务器 profile 切换（`logout` 只清当前状态）。
