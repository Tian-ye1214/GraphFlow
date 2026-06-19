# gf CLI 增强 + cli/ 包重构 + 技能按资源拆分 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `gf` CLI 重构成 `app/cli/` 包并补齐覆盖后端 API 的命令，再把单个 `gf-cli` 技能按资源拆成「总入口 + 5 个资源技能」。

**Architecture:** CLI 是 HTTP 瘦客户端（httpx，trust_env=False）。重构为包：`__init__.py` 放 main+状态原语、`client.py` 放 `Cli` 类与常量、`commands/*.py` 每资源一个模块各暴露 `register(sub)`。新增 1 个后端端点（数据集导出），复用 `app.services.export.export_rows`。

**Tech Stack:** Python / argparse / httpx / FastAPI / SQLAlchemy2 async / pytest（真实 uvicorn 集成测试）。

## Global Constraints

- 入口不变：`pyproject.toml` 的 `gf = "app.cli:main"` 必须继续有效——`app/cli/__init__.py` 必须定义/导出 `main`。
- 测试兼容：现有 `backend/tests/test_cli.py` 用 `import app.cli as cli` + `cli.main([...])` + `monkeypatch.setattr(cli, "STATE_FILE", ...)`；重构后 `cli.STATE_FILE`/`cli.main` 必须仍可从 `app.cli` 顶层访问，且 `STATE_FILE` 的 monkeypatch 能命中实际读取处。
- 向后兼容：现有所有命令拼写不变。
- 租户隔离：新端点用 `ds.user_id == user.id` 校验，越权返回 404。
- 密钥安全：任何输出/日志/响应绝不出现 api_key / Authorization 明文。
- KISS：最简实现，不为不发生的情况写防御代码。
- 中文输出：CLI 面向用户的提示与报错保持中文。
- 不走系统代理：新建 httpx 调用沿用 `trust_env=False`。
- 提交记录不含 "claude"，无 Co-Authored-By 尾注。
- 测试文件提交用 `git add -f`（`backend/tests` 在 .gitignore，新文件需 -f；现有文件多路径 add 也建议 -f）。
- 测试运行：`cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider <路径>`。

---

## File Structure

**重构后包结构（Task 1 建立）：**
- `app/cli/__init__.py` — `STATE_FILE`、`load_state()`、`save_state()`、`main(argv)`（组装 argparse + 异常处理）。**不在顶层 import commands**（main() 内惰性导入，避免循环）。
- `app/cli/client.py` — `Cli` 类、`die()`、`resolve`、`convert()`、`parse_kv()`、`find_node()`、`summarize()`、`watch_run()` 及所有常量表（NODE_TYPES/NODE_LABELS/KIND_LABELS/STATUS_LABELS/LLM_CONFIG_KEYS/LLM_PARAM_KEYS/INT_KEYS/FLOAT_KEYS/HTTP_STR_KEYS/MODEL_KEYS/OP_LABELS/build_op/_auto_node）。`Cli.__init__` 调 `from app.cli import load_state` 得到的 `load_state()`（该函数定义在 __init__，读 __init__ 的 STATE_FILE 全局，故 monkeypatch `cli.STATE_FILE` 生效）。
- `app/cli/commands/__init__.py` — 空（或列出模块）。
- `app/cli/commands/auth.py` — `register(sub)` + login/logout/st。
- `app/cli/commands/workflow.py` — register + wf(ls/add/rm/rename/restore)/use/show/cols/wf dump/wf load/node add/node rm/link/unlink。
- `app/cli/commands/node.py` — register + node set/node show/node prompt/op(add/ls/rm)。
- `app/cli/commands/model.py` — register + model(ls/add/set/rm/test)。
- `app/cli/commands/dataset.py` — register + data(ls/up/download/head/rm)。
- `app/cli/commands/run.py` — register + run/runs/watch/cancel/rerun/export/rows/logs/qc/rmrun。
- `app/cli/__main__.py` — `from app.cli import main; main()`。
- 删除：`app/cli.py`（被包取代）。

**后端：**
- `app/routers/datasets.py` — 新增 `export_dataset` 端点。

**技能（Task 10）：** `.claude/skills/` 下 gf-cli（改）+ gf-workflow/gf-node-prompt/gf-model/gf-dataset/gf-run（新）。

---

## Task 1: cli.py → cli/ 包重构（零行为变化）

**Files:**
- Create: `app/cli/__init__.py`, `app/cli/client.py`, `app/cli/__main__.py`, `app/cli/commands/__init__.py`, `app/cli/commands/{auth,workflow,node,model,dataset,run}.py`
- Delete: `app/cli.py`
- Test: 现有 `backend/tests/test_cli.py`（不改，作回归门）

**Interfaces:**
- Produces: `app.cli.main(argv)`、`app.cli.STATE_FILE`、`app.cli.load_state()`、`app.cli.save_state(state)`；`app.cli.client.Cli`、`app.cli.client.die(msg)`；每个 `commands/*.py` 的 `register(sub)`。

- [ ] **Step 1: 先确认现有 CLI 测试基线全绿**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py tests/test_cli_state_env.py`
Expected: 全 PASS（重构前基线）。

- [ ] **Step 2: 建 `app/cli/__init__.py`（状态原语 + main 组装）**

把原 `cli.py` 的 `STATE_FILE`/`load_state`/`save_state` 搬到这里；`main` 惰性导入各命令模块并组装。完整内容：

```python
"""gf —— GraphFlow 命令行客户端（包）。所有操作通过 HTTP API 完成，与前端同权限。"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

STATE_FILE = Path(os.environ.get("GF_STATE_FILE") or Path.home() / ".graphflow" / "cli.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None):
    from app.cli.client import die
    from app.cli.commands import auth, workflow, node, model, dataset, run
    p = argparse.ArgumentParser(prog="gf", description="GraphFlow 命令行客户端")
    sub = p.add_subparsers(dest="cmd", required=True)
    for mod in (auth, workflow, node, model, dataset, run):
        mod.register(sub)
    args = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # 启用 conhost ANSI（watch 进度刷新用）
    try:
        args.func(args)
    except httpx.ConnectError:
        die("无法连接服务器，请确认 GraphFlow 已启动")
    except ValueError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
```

- [ ] **Step 3: 建 `app/cli/client.py`（Cli 类 + 公共工具 + 常量）**

把原 `cli.py` 里这些**逐字搬过来**：`die`、`Cli` 类（含 check/req/resolve/current_wf/get_wf/put_graph）、常量表（NODE_TYPES、NODE_LABELS、KIND_LABELS、STATUS_LABELS、LLM_CONFIG_KEYS、LLM_PARAM_KEYS、INT_KEYS、FLOAT_KEYS、HTTP_STR_KEYS、MODEL_KEYS、OP_LABELS）、`convert`、`parse_kv`、`find_node`、`summarize`、`build_op`、`_auto_node`、`watch_run`。文件头：

```python
"""gf 公共：HTTP 客户端、资源解析、参数转换、常量表。"""
import json
import sys
import time
from pathlib import Path

import httpx

from app.cli import load_state, save_state, STATE_FILE  # noqa: F401  状态原语由包顶层提供


def die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)
```

`Cli.__init__` 保持原逻辑，但 `load_state()` 现在来自上面的 import（它读 `app.cli.STATE_FILE`，monkeypatch 生效）：

```python
class Cli:
    def __init__(self):
        self.state = load_state()
        if not self.state.get("cookie"):
            die("未登录，先执行: gf login <用户名>")
        self.http = httpx.Client(base_url=self.state["server"], trust_env=False,
                                 cookies={"gf_session": self.state["cookie"]}, timeout=30)
    # check / req / resolve / current_wf / get_wf / put_graph 逐字搬自原 cli.py
```

> 循环依赖说明：`client.py` 顶层 `from app.cli import load_state, ...` 在 `main()` 惰性导入 commands→client 时执行，此时 `app.cli`（__init__）已加载完毕，不会循环。

- [ ] **Step 4: 建 `app/cli/__main__.py`**

```python
from app.cli import main

main()
```

- [ ] **Step 5: 建 `app/cli/commands/auth.py`（register 范式样板）**

把原 `cmd_login`/`cmd_st` 逐字搬来，新增 `register`：

```python
"""认证与会话：login / logout / st。"""
import httpx

from app.cli import load_state, save_state
from app.cli.client import Cli, die


def cmd_login(args):
    server = args.server.rstrip("/")
    r = httpx.post(f"{server}/api/auth/login", json={"username": args.username},
                   timeout=10, trust_env=False)
    if r.status_code >= 400:
        die(f"登录失败: HTTP {r.status_code} {r.text[:200]}")
    state = load_state()
    state.update(server=server, cookie=r.cookies.get("gf_session"))
    save_state(state)
    print(f"已登录 {args.username} @ {server}")


def cmd_st(args):
    cli = Cli()
    me = cli.req("GET", "/api/me")
    line = f"服务器 {cli.state['server']}  用户 {me['username']}"
    wf_id = cli.state.get("workflow_id")
    if wf_id:
        wf = cli.req("GET", f"/api/workflows/{wf_id}")
        line += f"  当前工作流 {wf['name']}（#{wf_id}）"
    print(line)


def register(sub):
    s = sub.add_parser("login", help="登录")
    s.add_argument("username")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("st", help="当前状态")
    s.set_defaults(func=cmd_st)
```

（`logout` 在 Task 2 加。）

- [ ] **Step 6: 建其余命令模块，逐字搬迁 + 各写 register**

按 File Structure 把原 `cli.py` 剩余 `cmd_*` 函数分配到 `workflow.py`/`node.py`/`model.py`/`dataset.py`/`run.py`，每个模块顶部 `from app.cli.client import Cli, die, find_node, ...`（按需），并把原 `main()` 里对应的 `sub.add_parser(...)` 块搬进各模块的 `register(sub)`。**只搬不改逻辑。** 节点相关的 `link`/`unlink`/`node add`/`node rm`/`show`/`use`/`cols 占位` 放 `workflow.py`；`node set`/`node show`/`op` 放 `node.py`。

- [ ] **Step 7: 删除 `app/cli.py`**

```bash
git rm backend/app/cli.py
```

- [ ] **Step 8: 跑回归确认零行为变化**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py tests/test_cli_state_env.py`
Expected: 全 PASS（与 Step 1 数量一致）。再跑 `python -c "import app.cli; print(app.cli.main)"` 确认入口可导入。

- [ ] **Step 9: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/ && git rm backend/app/cli.py
git commit -m "refactor(gf): cli.py 拆成 cli/ 包（按资源分模块，零行为变化）"
```

---

## Task 2: `gf logout`

**Files:**
- Modify: `app/cli/commands/auth.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `app.cli.load_state/save_state`, `Cli`。
- Produces: `cmd_logout(args)`；命令 `gf logout`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cli.py`：

```python
def test_logout_clears_state(server, capsys):
    gf("login", "tester", "--server", server)
    gf("wf", "add", "流X"); gf("use", "流X")
    capsys.readouterr()
    gf("logout")
    assert "已登出" in capsys.readouterr().out
    state = json.loads(cli.STATE_FILE.read_text(encoding="utf-8"))
    assert not state.get("cookie") and not state.get("workflow_id")
    assert state.get("server") == server   # server 保留，方便重登
    with pytest.raises(SystemExit):        # 登出后需重新登录
        gf("st")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_logout_clears_state`
Expected: FAIL（`invalid choice: 'logout'`，argparse 退出码 2）。

- [ ] **Step 3: 实现 logout**

在 `auth.py` 加：

```python
def cmd_logout(args):
    cli = Cli()
    try:
        cli.req("POST", "/api/auth/logout")
    except SystemExit:
        pass  # 服务器端登出失败也要清本地状态
    state = load_state()
    state.pop("cookie", None)
    state.pop("workflow_id", None)
    save_state(state)
    print("已登出")
```

`register(sub)` 末尾加：

```python
    s = sub.add_parser("logout", help="登出并清本地状态")
    s.set_defaults(func=cmd_logout)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_logout_clears_state`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/auth.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): gf logout 登出并清本地状态"
```

---

## Task 3: workflow 命令 —— `wf rename` / `cols` / `wf dump` / `wf load`

**Files:**
- Modify: `app/cli/commands/workflow.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Cli`（resolve/current_wf/get_wf/put_graph/req）。
- Produces: `cmd_wf_rename`、`cmd_cols`、`cmd_wf_dump`、`cmd_wf_load`；命令 `gf wf rename/dump/load`、`gf cols`。

- [ ] **Step 1: 写失败测试**

```python
def test_wf_rename(server, capsys):
    login_and_wf(server, "旧名")
    gf("wf", "rename", "旧名", "新名")
    capsys.readouterr()
    gf("wf", "ls")
    out = capsys.readouterr().out
    assert "新名" in out and "旧名" not in out


def test_cols_shows_lineage(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n', encoding="utf-8")
    gf("data", "up", str(seed))
    gf("wf", "add", "血缘流"); gf("use", "血缘流")
    gf("node", "add", "input"); gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm"); gf("node", "set", "llm_synth_1", "out=a")
    gf("link", "input_1", "llm_synth_1")
    capsys.readouterr()
    gf("cols")
    out = capsys.readouterr().out
    assert "llm_synth_1" in out and "q" in out and "a" in out


def test_wf_dump_load_roundtrip(server, capsys, tmp_path):
    login_and_wf(server, "导出流")
    gf("node", "add", "input"); gf("node", "add", "output")
    gf("link", "input_1", "output_1")
    dump = tmp_path / "graph.json"
    gf("wf", "dump", "-o", str(dump))
    graph = json.loads(dump.read_text(encoding="utf-8"))
    assert {n["id"] for n in graph["nodes"]} == {"input_1", "output_1"}
    # 改名后 load 回去
    gf("wf", "add", "空流"); gf("use", "空流")
    gf("wf", "load", str(dump))
    capsys.readouterr()
    gf("show")
    assert "input_1 -> output_1" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_wf_rename tests/test_cli.py::test_cols_shows_lineage tests/test_cli.py::test_wf_dump_load_roundtrip`
Expected: FAIL（无 rename/cols/dump/load 子命令）。

- [ ] **Step 3: 实现**

在 `workflow.py` 加：

```python
def cmd_wf_rename(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    cli.req("PUT", f"/api/workflows/{wf_id}", json={"name": args.name})
    print(f"已重命名工作流 #{wf_id} -> {args.name}")


def cmd_cols(args):
    cli = Cli()
    wf_id = cli.current_wf()
    cols = cli.req("GET", f"/api/workflows/{wf_id}/columns")
    items = {args.node: cols[args.node]} if args.node else cols
    if args.node and args.node not in cols:
        die(f"节点 {args.node} 不存在")
    for nid, io in items.items():
        print(f"{nid}")
        print(f"  输入: {', '.join(io['input']) or '（无）'}")
        print(f"  输出: {', '.join(io['output']) or '（无）'}")


def cmd_wf_dump(args):
    cli = Cli()
    wf = cli.get_wf()
    out = Path(args.output or f"{wf['name']}.json")
    out.write_text(json.dumps(wf["graph"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出工作流图到 {out}")


def cmd_wf_load(args):
    cli = Cli()
    path = Path(args.file)
    if not path.is_file():
        die(f"文件不存在: {args.file}")
    graph = json.loads(path.read_text(encoding="utf-8"))
    wf_id = cli.current_wf()
    cli.put_graph(wf_id, graph)
    print(f"已从 {args.file} 载入工作流图（#{wf_id}）")
```

`workflow.py` 顶部需 `import json` 与 `from pathlib import Path`。在 `register(sub)` 的 wf 子分组里加 rename，并加 cols/dump/load：

```python
    # 在 wf 子解析器组内：
    s = wf.add_parser("rename"); s.add_argument("ref"); s.add_argument("name"); s.set_defaults(func=cmd_wf_rename)
    s = wf.add_parser("dump"); s.add_argument("-o", "--output"); s.set_defaults(func=cmd_wf_dump)
    s = wf.add_parser("load"); s.add_argument("file"); s.set_defaults(func=cmd_wf_load)
    # 顶层：
    s = sub.add_parser("cols", help="列血缘（各节点输入/输出列）")
    s.add_argument("node", nargs="?")
    s.set_defaults(func=cmd_cols)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_wf_rename tests/test_cli.py::test_cols_shows_lineage tests/test_cli.py::test_wf_dump_load_roundtrip`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/workflow.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): wf rename / cols 列血缘 / wf dump-load 整图导入导出"
```

---

## Task 4: `gf node prompt`（从文件 / 编辑器 / stdin 写提示词）

**Files:**
- Modify: `app/cli/commands/node.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Cli`（get_wf/put_graph）、`find_node`。
- Produces: `cmd_node_prompt(args)`；命令 `gf node prompt <id> (--system|--user) (--file F | --edit | -)`。

- [ ] **Step 1: 写失败测试**

```python
def test_node_prompt_from_file(server, capsys, tmp_path):
    login_and_wf(server)
    gf("node", "add", "llm")
    pf = tmp_path / "p.md"
    pf.write_text("# 指令\n把 {{q}} 翻译成英文\n", encoding="utf-8")
    gf("node", "prompt", "llm_synth_1", "--user", "--file", str(pf))
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["user_prompt"] == "# 指令\n把 {{q}} 翻译成英文\n"


def test_node_prompt_from_stdin(server, capsys, monkeypatch, tmp_path):
    import io
    login_and_wf(server)
    gf("node", "add", "qc")
    monkeypatch.setattr("sys.stdin", io.StringIO("判定规则：必须为 JSON"))
    gf("node", "prompt", "qc_1", "--system", "-")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    node = json.loads(capsys.readouterr().out)
    assert node["config"]["system_prompt"] == "判定规则：必须为 JSON"


def test_node_prompt_requires_field_and_source(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    with pytest.raises(SystemExit) as e:   # 缺 --system/--user：argparse 互斥必填
        gf("node", "prompt", "llm_synth_1", "--file", "x")
    assert e.value.code == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_node_prompt_from_file tests/test_cli.py::test_node_prompt_from_stdin tests/test_cli.py::test_node_prompt_requires_field_and_source`
Expected: FAIL（无 `node prompt`）。

- [ ] **Step 3: 实现**

`node.py` 顶部加 `import os`, `import sys`, `import subprocess`, `import tempfile`, `from pathlib import Path`。加：

```python
def _read_prompt(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.edit:
        editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "vi")
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
            tmp = f.name
        subprocess.call([editor, tmp])
        return Path(tmp).read_text(encoding="utf-8")
    return sys.stdin.read()   # args.from_stdin


def cmd_node_prompt(args):
    cli = Cli()
    wf = cli.get_wf()
    node = find_node(wf["graph"], args.id)
    field = "system_prompt" if args.system else "user_prompt"
    node["config"][field] = _read_prompt(args)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已写入 {args.id} 的 {field}（{len(node['config'][field])} 字符）")
```

`register(sub)` 里 node 子分组加（互斥组保证「字段」与「来源」各必选其一）：

```python
    s = node.add_parser("prompt")
    s.add_argument("id")
    g1 = s.add_mutually_exclusive_group(required=True)
    g1.add_argument("--system", action="store_true")
    g1.add_argument("--user", action="store_true")
    g2 = s.add_mutually_exclusive_group(required=True)
    g2.add_argument("--file")
    g2.add_argument("--edit", action="store_true")
    g2.add_argument("-", dest="from_stdin", action="store_true")
    s.set_defaults(func=cmd_node_prompt)
```

> 注意：`-` 作为 argparse 选项名要用 `dest="from_stdin"`；`_read_prompt` 的 stdin 分支即默认分支（既非 file 也非 edit）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_node_prompt_from_file tests/test_cli.py::test_node_prompt_from_stdin tests/test_cli.py::test_node_prompt_requires_field_and_source`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/node.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): node prompt 从文件/编辑器/stdin 写入长提示词"
```

---

## Task 5: `gf node set` 补齐配置键（drop/status_col/feedback_col/outs/think/effort/headers）

**Files:**
- Modify: `app/cli/commands/node.py`（`cmd_node_set`）
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: 既有 `cmd_node_set` 的 key 分派逻辑。
- Produces: `cmd_node_set` 新支持 7 个键。

- [ ] **Step 1: 写失败测试**

```python
def test_node_set_new_keys(server, capsys):
    login_and_wf(server)
    gf("model", "add", "m", "--url", "http://x/v1", "--model", "q", "--key", "k")
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "drop=secret,tmp", "outs=q_en,cat_en",
       "think=on", "effort=high")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["drop_columns"] == ["secret", "tmp"]
    assert c["output_columns"] == ["q_en", "cat_en"]
    assert c["params"]["thinking_enabled"] is True
    assert c["params"]["reasoning_effort"] == "high"


def test_node_set_qc_status_feedback_cols(server, capsys):
    login_and_wf(server)
    gf("node", "add", "qc")
    gf("node", "set", "qc_1", "status_col=verdict", "feedback_col=fb")
    capsys.readouterr()
    gf("node", "show", "qc_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["status_column"] == "verdict" and c["feedback_column"] == "fb"


def test_node_set_http_headers(server, capsys):
    login_and_wf(server)
    gf("node", "add", "http")
    gf("node", "set", "http_fetch_1", "headers=Authorization:Bearer x,X-Tag:demo")
    capsys.readouterr()
    gf("node", "show", "http_fetch_1")
    c = json.loads(capsys.readouterr().out)["config"]
    assert c["headers"] == {"Authorization": "Bearer x", "X-Tag": "demo"}


def test_node_set_think_off(server, capsys):
    login_and_wf(server)
    gf("node", "add", "llm")
    gf("node", "set", "llm_synth_1", "think=off")
    capsys.readouterr()
    gf("node", "show", "llm_synth_1")
    assert json.loads(capsys.readouterr().out)["config"]["params"]["thinking_enabled"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k "new_keys or status_feedback or http_headers or think_off"`
Expected: FAIL（这些键报「未知配置键」，退出码 1）。

- [ ] **Step 3: 实现**

在 `cmd_node_set` 的 key 分派链里，于 `else: die(f"未知配置键 {k}")` 之前插入分支：

```python
        elif k == "drop":
            cfg["drop_columns"] = [c for c in v.split(",") if c]
        elif k == "outs":
            cfg["output_columns"] = [c for c in v.split(",") if c]
        elif k == "status_col":
            cfg["status_column"] = v
        elif k == "feedback_col":
            cfg["feedback_column"] = v
        elif k == "think":
            cfg.setdefault("params", {})["thinking_enabled"] = v.lower() in ("on", "true", "1", "yes")
        elif k == "effort":
            cfg.setdefault("params", {})["reasoning_effort"] = v
        elif k == "headers":
            cfg["headers"] = dict(p.split(":", 1) for p in v.split(",") if ":" in p)
```

> `headers` 值用「逗号分隔多对、冒号分键值」，与既有 `extract` 同款解析。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k "new_keys or status_feedback or http_headers or think_off"`
Expected: PASS（4 个）。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/node.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): node set 补 drop/outs/status_col/feedback_col/think/effort/headers"
```

---

## Task 6: 数据集导出端点 + `gf data download`

**Files:**
- Modify: `app/routers/datasets.py`（新增 `export_dataset`）
- Modify: `app/cli/commands/dataset.py`（新增 `cmd_data_download`）
- Test: `backend/tests/test_datasets.py`（端点）、`backend/tests/test_cli.py`（CLI）

**Interfaces:**
- Consumes: `app.services.export.export_rows(rows, fmt, path)`、`datasets._get_owned`、`DatasetRow`。
- Produces: `GET /api/datasets/{ds_id}/export?format=`；`cmd_data_download`；命令 `gf data download <ref> [-o 文件] [--format ...]`。

- [ ] **Step 1: 写失败测试（端点）**

追加到 `tests/test_datasets.py`：

```python
async def test_export_dataset_jsonl(auth_client):
    import json as _json
    ds = (await upload(auth_client, ("导出集.jsonl", JSONL))).json()[0]
    r = await auth_client.get(f"/api/datasets/{ds['id']}/export", params={"format": "jsonl"})
    assert r.status_code == 200
    lines = [l for l in r.text.splitlines() if l]
    assert len(lines) == 3 and _json.loads(lines[0])["q"] == "你好"


async def test_export_dataset_csv(auth_client):
    ds = (await upload(auth_client, ("c.jsonl", JSONL))).json()[0]
    r = await auth_client.get(f"/api/datasets/{ds['id']}/export", params={"format": "csv"})
    assert r.status_code == 200 and "q" in r.text and "你好" in r.text


async def test_export_dataset_rejects_foreign(auth_client, session_factory):
    from sqlalchemy import select
    from app.models import User
    async with session_factory() as s:
        stranger = User(username="ds_stranger", display_name="x")
        s.add(stranger); await s.commit()
        ds = Dataset(user_id=stranger.id, name="他人集", row_count=0, columns_json="[]")
        s.add(ds); await s.commit(); did = ds.id
    assert (await auth_client.get(f"/api/datasets/{did}/export")).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_datasets.py -k export_dataset`
Expected: FAIL（404，端点不存在）。

- [ ] **Step 3: 实现端点**

`datasets.py` 顶部 import 增补：`import asyncio`、`from typing import Literal`、`from fastapi.responses import FileResponse`、`from app.services.export import export_rows`。在 `delete_dataset` 之前加：

```python
@router.get("/{ds_id}/export")
async def export_dataset(ds_id: int, format: Literal["jsonl", "csv", "xlsx"] = "jsonl",
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    ds = await _get_owned(ds_id, user, session)
    recs = (await session.execute(select(DatasetRow).where(
        DatasetRow.dataset_id == ds.id).order_by(DatasetRow.idx))).scalars().all()
    rows = [json.loads(r.data_json) for r in recs]
    safe = re.sub(r'[\\/:*?"<>|]', "_", ds.name)   # 与 upload 同款清洗，杜绝路径穿越
    filename = f"{safe}.{format}"
    path = await asyncio.to_thread(
        export_rows, rows, format, settings.data_dir / "exports" / filename)
    return FileResponse(path, filename=filename)
```

> 文件名清洗复用本模块已 import 的 `re`（正则与 `upload` 里的 `safe_name` 完全一致）。

- [ ] **Step 4: 跑端点测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_datasets.py -k export_dataset`
Expected: PASS（3 个）。

- [ ] **Step 5: 写 CLI 失败测试**

追加到 `tests/test_cli.py`：

```python
def test_data_download(server, capsys, tmp_path):
    gf("login", "tester", "--server", server)
    seed = tmp_path / "下载集.jsonl"
    seed.write_text('{"q": "甲"}\n{"q": "乙"}\n', encoding="utf-8")
    gf("data", "up", str(seed))
    out = tmp_path / "out.jsonl"
    capsys.readouterr()
    gf("data", "download", "下载集", "-o", str(out))
    assert "已下载" in capsys.readouterr().out
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2 and lines[0]["q"] == "甲"
```

- [ ] **Step 6: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_data_download`
Expected: FAIL（无 `data download`）。

- [ ] **Step 7: 实现 CLI**

`dataset.py` 顶部需 `from pathlib import Path`。加：

```python
def cmd_data_download(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    r = cli.check(cli.http.get(f"/api/datasets/{ds_id}/export", params={"format": args.format}))
    out = Path(args.output or f"{args.ref}.{args.format}")
    out.write_bytes(r.content)
    print(f"已下载 {out}（{len(r.content)} 字节）")
```

`register(sub)` 的 data 子分组加：

```python
    s = data.add_parser("download")
    s.add_argument("ref"); s.add_argument("-o", "--output")
    s.add_argument("--format", default="jsonl", choices=["jsonl", "csv", "xlsx"])
    s.set_defaults(func=cmd_data_download)
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_data_download`
Expected: PASS。

- [ ] **Step 9: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/routers/datasets.py backend/app/cli/commands/dataset.py
git add -f backend/tests/test_datasets.py backend/tests/test_cli.py
git commit -m "feat(gf): 数据集导出端点 + gf data download"
```

---

## Task 7: `gf rows` / `gf logs`

**Files:**
- Modify: `app/cli/commands/run.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Cli`（req）；`GET /runs/{id}`（取 graph 定位默认输出节点）、`/runs/{id}/rows`、`/runs/{id}/logs`、`/runs/{id}/model-logs`。
- Produces: `cmd_rows`、`cmd_logs`；命令 `gf rows`、`gf logs`。

- [ ] **Step 1: 写失败测试**

接在 `test_cli_full_chain` 之后（复用同款 fake_chat 链建运行）。新增独立测试：

```python
def _build_and_run(server, tmp_path, monkeypatch):
    from app.services import llm

    async def fake_chat(mc, system, user, params=None, retries=3):
        return f"答[{user}]", {"prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(llm, "chat", fake_chat)
    seed = tmp_path / "种子.jsonl"
    seed.write_text('{"q": "问0"}\n{"q": "问1"}\n', encoding="utf-8")
    gf("login", "tester", "--server", server)
    gf("model", "add", "通义", "--url", "http://x/v1", "--model", "qwen", "--key", "k")
    gf("data", "up", str(seed))
    gf("wf", "add", "链"); gf("use", "链")
    gf("node", "add", "input"); gf("node", "set", "input_1", "dataset=种子")
    gf("node", "add", "llm"); gf("node", "set", "llm_synth_1", "prompt=Q:{{q}}", "model=通义", "out=a")
    gf("node", "add", "output")
    gf("link", "input_1", "llm_synth_1"); gf("link", "llm_synth_1", "output_1")
    gf("run", "-f")


def test_rows_default_output_node(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("rows", "1")
    out = capsys.readouterr().out
    assert "答[Q:问0]" in out and "答[Q:问1]" in out


def test_rows_specific_node(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("rows", "1", "--node", "input_1")
    assert "问0" in capsys.readouterr().out


def test_logs_shows_timeline(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("logs", "1")
    out = capsys.readouterr().out
    assert out.strip()   # 至少有日志行（含节点名/级别）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k "rows_default or rows_specific or logs_shows"`
Expected: FAIL（无 rows/logs）。

- [ ] **Step 3: 实现**

`run.py` 顶部需 `import json`、`from app.cli.client import Cli, die, STATUS_LABELS`。加：

```python
def _default_output_node(cli: Cli, run_id: int) -> str:
    d = cli.req("GET", f"/api/runs/{run_id}")
    outs = [n for n in d["graph"]["nodes"] if n["type"] == "output"]
    if not outs:
        die("该运行的工作流没有输出节点，请用 --node 指定")
    return outs[0]["id"]


def cmd_rows(args):
    cli = Cli()
    node = args.node or _default_output_node(cli, args.run_id)
    status = "failed" if args.failed else "done"
    page = cli.req("GET", f"/api/runs/{args.run_id}/rows",
                   params={"node_id": node, "status": status, "page": args.page, "page_size": 20})
    print(f"运行 #{args.run_id} 节点 {node} {status} 行（共 {page['total']}，第 {args.page} 页）")
    for row in page["rows"]:
        print(json.dumps(row, ensure_ascii=False))


def cmd_logs(args):
    cli = Cli()
    if args.model:
        for m in cli.req("GET", f"/api/runs/{args.run_id}/model-logs"):
            print(f"[{m['source']}] {m['node_id'] or '-'}  {m['model_name']}")
    else:
        for l in cli.req("GET", f"/api/runs/{args.run_id}/logs"):
            print(f"[{l['created_at'][:19]}] {l['level'].upper()} {l['node_id'] or '-'}  {l['message']}")
```

`register(sub)` 加：

```python
    s = sub.add_parser("rows", help="看运行某节点的结果行")
    s.add_argument("run_id", type=int)
    s.add_argument("--node"); s.add_argument("--failed", action="store_true")
    s.add_argument("--page", type=int, default=1)
    s.set_defaults(func=cmd_rows)

    s = sub.add_parser("logs", help="看运行日志（--model 看模型对话）")
    s.add_argument("run_id", type=int); s.add_argument("--model", action="store_true")
    s.set_defaults(func=cmd_logs)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k "rows_default or rows_specific or logs_shows"`
Expected: PASS（3 个）。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/run.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): gf rows 看结果行 / gf logs 看运行日志与模型对话"
```

---

## Task 8: `gf qc`（质检指标 + 失败样本 + --download）

**Files:**
- Modify: `app/cli/commands/run.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Cli`；`/runs/{id}/qc-metrics`、`/runs/{id}/qc-failures`、`/runs/{id}/qc-failures.jsonl`。
- Produces: `cmd_qc`；命令 `gf qc <run_id> [--download [-o 文件]]`。

- [ ] **Step 1: 写失败测试**

直接给运行注入 QcMetric/QcFailure 后验证打印与下载（不必真跑质检链）：

```python
def test_qc_prints_metrics_and_failures(server, capsys, tmp_path):
    import json as _json
    from app.config import settings as _s
    from app.db import get_session_factory
    from app.models import Run, User, QcMetric, QcFailure
    from sqlalchemy import select
    import asyncio

    gf("login", "tester", "--server", server)

    async def seed():
        sf = get_session_factory()
        async with sf() as s:
            uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
            run = Run(user_id=uid, workflow_id=0, workflow_version_id=0, status="completed")
            s.add(run); await s.commit(); rid = run.id
            s.add(QcMetric(run_id=rid, node_id="qc1", total=10, first_round_pass=6))
            s.add(QcFailure(run_id=rid, node_id="qc1", sample_json='{"q":"x"}',
                            reasons_json=_json.dumps([{"model_config_id": 1, "status": "failed", "reason": "短"}])))
            await s.commit()
            return rid

    rid = asyncio.get_event_loop().run_until_complete(seed())
    capsys.readouterr()
    gf("qc", str(rid))
    out = capsys.readouterr().out
    assert "60" in out and "短" in out   # 首轮通过率 60% + 失败原因
    dl = tmp_path / "fail.jsonl"
    gf("qc", str(rid), "--download", "-o", str(dl))
    rec = _json.loads(dl.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["_qc_model_1"] == "failed"
```

> 若 `asyncio.get_event_loop().run_until_complete` 在该 pytest 环境报 deprecation/无 loop，改用 `asyncio.new_event_loop().run_until_complete(seed())`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_qc_prints_metrics_and_failures`
Expected: FAIL（无 `qc` 命令）。

- [ ] **Step 3: 实现**

`run.py` 加（`from pathlib import Path` 若未导入则补）：

```python
def cmd_qc(args):
    cli = Cli()
    if args.download:
        r = cli.check(cli.http.get(f"/api/runs/{args.run_id}/qc-failures.jsonl"))
        out = Path(args.output or f"run{args.run_id}_qc_failures.jsonl")
        out.write_bytes(r.content)
        print(f"已下载失败样本 {out}（{len(r.content)} 字节）")
        return
    for m in cli.req("GET", f"/api/runs/{args.run_id}/qc-metrics"):
        print(f"{m['node_id']}  首轮通过 {m['first_round_pass']}/{m['total']}"
              f"（{round(m['first_round_rate'] * 100)}%）")
    fails = cli.req("GET", f"/api/runs/{args.run_id}/qc-failures")
    print(f"失败样本（{len(fails)}）:")
    for f in fails:
        reasons = "；".join(f"{r.get('status', '')}:{r['reason']}" for r in f["reasons"])
        print(f"  {json.dumps(f['sample'], ensure_ascii=False)}  -> {reasons}")
```

`register(sub)` 加：

```python
    s = sub.add_parser("qc", help="看质检指标+失败样本（--download 落 jsonl）")
    s.add_argument("run_id", type=int)
    s.add_argument("--download", action="store_true"); s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_qc)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py::test_qc_prints_metrics_and_failures`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/run.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): gf qc 看质检指标/失败样本，--download 落 jsonl"
```

---

## Task 9: `gf rmrun`（删单次 / `--all` 清空）

**Files:**
- Modify: `app/cli/commands/run.py`
- Test: `backend/tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `Cli`；`DELETE /runs/{id}`、`DELETE /runs`。
- Produces: `cmd_rmrun`；命令 `gf rmrun <run_id>` / `gf rmrun --all`。

- [ ] **Step 1: 写失败测试**

```python
def test_rmrun_single_and_all(server, capsys, tmp_path, monkeypatch):
    _build_and_run(server, tmp_path, monkeypatch)   # 产生运行 #1
    capsys.readouterr()
    gf("rmrun", "1")
    assert "已删除运行 #1" in capsys.readouterr().out
    gf("runs")
    assert "链" not in capsys.readouterr().out
    # 再跑一次产生新运行，--all 清空
    _build_and_run(server, tmp_path, monkeypatch)
    capsys.readouterr()
    gf("rmrun", "--all")
    assert "已清空" in capsys.readouterr().out
    gf("runs")
    assert capsys.readouterr().out.strip() == ""


def test_rmrun_requires_id_or_all(server, capsys):
    gf("login", "tester", "--server", server)
    with pytest.raises(SystemExit) as e:
        gf("rmrun")
    assert e.value.code == 2   # 既无 run_id 也无 --all
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k rmrun`
Expected: FAIL（无 rmrun）。

- [ ] **Step 3: 实现**

`run.py` 加：

```python
def cmd_rmrun(args):
    cli = Cli()
    if args.all:
        r = cli.req("DELETE", "/api/runs")
        print(f"已清空 {r['deleted']} 次运行")
    elif args.run_id is not None:
        cli.req("DELETE", f"/api/runs/{args.run_id}")
        print(f"已删除运行 #{args.run_id}")
    else:
        die("需给运行 ID 或 --all")
```

`register(sub)` 加（`run_id` 设为可选位置参数，配合 `--all`；缺二者时由 cmd 内 die，但「都不给」应是用法错误退出码 2——故用自定义校验：argparse 层 run_id 可选、--all 可选，二者皆缺在 cmd 里 die 退出码 1。若要退出码 2 见下）：

```python
    s = sub.add_parser("rmrun", help="删运行（给 ID 删单次，--all 清空全部）")
    s.add_argument("run_id", type=int, nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_rmrun)
```

> 关于 `test_rmrun_requires_id_or_all` 期望退出码 2：argparse 无法天然表达「二选一必填」。实现改为：在 `cmd_rmrun` 里当 `not args.all and args.run_id is None` 时 `cli` 未建，直接 `import sys; print("用法: gf rmrun <run_id> | --all", file=sys.stderr); sys.exit(2)`。把该 die 分支换成 `sys.exit(2)` 以满足测试：

```python
def cmd_rmrun(args):
    import sys
    if not args.all and args.run_id is None:
        print("用法: gf rmrun <run_id> | --all", file=sys.stderr)
        sys.exit(2)
    cli = Cli()
    if args.all:
        r = cli.req("DELETE", "/api/runs")
        print(f"已清空 {r['deleted']} 次运行")
    else:
        cli.req("DELETE", f"/api/runs/{args.run_id}")
        print(f"已删除运行 #{args.run_id}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py -k rmrun`
Expected: PASS（2 个）。

- [ ] **Step 5: 跑全部 CLI 测试回归**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_cli.py tests/test_cli_state_env.py tests/test_datasets.py`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/run.py && git add -f backend/tests/test_cli.py
git commit -m "feat(gf): gf rmrun 删单次运行 / --all 清空"
```

---

## Task 10: 技能按资源拆分（gf-cli 总入口 + 5 资源技能）

**Files:**
- Modify: `.claude/skills/gf-cli/SKILL.md`（瘦身为总入口）
- Delete: `.claude/skills/gf-cli/reference.md`（内容分流到各技能）
- Create: `.claude/skills/gf-workflow/SKILL.md`、`gf-node-prompt/SKILL.md`、`gf-model/SKILL.md`、`gf-dataset/SKILL.md`、`gf-run/SKILL.md`
- 保留/移动：`.claude/skills/gf-cli/scripts/build-pipeline.ps1`（端到端示例留 gf-cli）

**Interfaces:**
- Produces: 6 个技能，每个独立 SKILL.md，frontmatter `name`/`description` 精准触发。

> 本任务是文档，无自动化测试；以「人工核对命令拼写/键名表与实现一致 + 每个 SKILL.md 能独立读懂」为验收。

- [ ] **Step 1: 写 gf-cli 总入口 SKILL.md**

内容：总览（瘦客户端、与前端同权限、SSE 联动）、安装（`uv tool install -e .`）、`login`/`logout`/`st`、**核心流程** `login → use → 建图 → run -f`、**路由表**（建图拓扑→gf-workflow；节点配置/提示词→gf-node-prompt；模型→gf-model；数据集→gf-dataset；运行与检查→gf-run）、跨域坑（jsonl 别带 BOM 的 PowerShell 写法、resolve 纯数字按 ID/否则按名重名报错、退出码 1/2/130、服务器没起怎么起、不走系统代理）。frontmatter description 覆盖「不知道该用哪个 gf 子命令 / gf 报错 / 总览」。

- [ ] **Step 2: 写 gf-workflow SKILL.md**

命令：`wf ls/add/rm/rename/restore`、`use`、`show`、`cols`、`wf dump/load`、`node add/rm`、`link/unlink`。重点：节点自动编号用类型全名（llm→llm_synth_1）、`link --kind rescan` 必须从 qc 出发、`cols` 查 `{{列}}`、dump/load 整图 JSON。description 触发「搭/改工作流结构、连线、看图、列血缘、整图导入导出」。

- [ ] **Step 3: 写 gf-node-prompt SKILL.md**

命令：`node set`（**逐键作用表**，见下）、`node show`、`node prompt`（--file/--edit/-）、`op`（8 种语法）、qc 回扫。`node set` 键名表每行写「设什么 / 键(别名) / 实际字段 / 取值示例 / 适用节点类型」，**必须含本次新增**：

| 设什么 | 键 | 实际字段 | 示例 | 适用 |
|---|---|---|---|---|
| 数据集 | `dataset=名1,名2` | dataset_ids | `dataset=种子` | input |
| 模型 | `model=名或ID` | model_config_id | `model=通义` | llm/qc |
| 系统/用户提示词 | `system=`/`prompt=` | system_prompt/user_prompt | `"prompt=回答:{{q}}"` | llm/qc |
| 输出列/模式 | `out=`/`mode=column或json` | output_column/output_mode | `out=a` | llm |
| JSON 多输出列 | `outs=q_en,cat_en` | output_columns | `outs=q_en,cat_en` | llm(mode=json) |
| 扇出/并发/重试 | `fanout=`/`conc=`/`retries=` | fanout_n/concurrency/retries | `conc=4` | llm/qc/http |
| 采样参数 | `temp=`/`top_p=`/`max_tokens=`/`timeout=`/`json_mode=` | params.* | `temp=0` | llm/qc |
| 思考 | `think=on\|off`/`effort=low..max` | params.thinking_enabled/reasoning_effort | `think=on effort=high` | llm/qc/agent |
| 删列 | `drop=列1,列2` | drop_columns | `drop=secret` | 任意 |
| 质检状态列 | `status_col=名` | status_column | `status_col=verdict` | qc |
| 质检反馈列 | `feedback_col=名` | feedback_column | `feedback_col=fb` | qc |
| 质检判定/轮数 | `judge_models=名1,名2`/`pass_k=`/`max_rounds=` | judge_model_ids/pass_k/max_rounds | `pass_k=2` | qc |
| 输出存数据集 | `save_as=名`（空串关闭） | save_as_dataset+dataset_name | `save_as=结果集` | output |
| HTTP url/方法/体 | `url=`/`method=`/`body=` | url/method/body | `url=http://api/{{q}}` | http |
| HTTP 提取 | `extract=列:路径,...` | extract | `extract=temp:data.temp` | http |
| HTTP 请求头 | `headers=K1:V1,K2:V2` | headers | `headers=Authorization:Bearer x` | http |

并注明：质检判定 JSON 契约是 `{"status":"pass"|"failed"|...,"reason":...}`（只有 "pass" 算通过），**不要写旧的 `{"pass":true}`**；长提示词用 `gf node prompt <id> --user --file p.md`。description 触发「配置节点、写提示词、自动处理 op、质检回扫、报『未知配置键』」。

- [ ] **Step 4: 写 gf-model / gf-dataset / gf-run SKILL.md**

- gf-model：`model ls/add/set/rm/test`（永不显示明文 key；set 键 name/model/url/key + temp/top_p/max_tokens）。
- gf-dataset：`data ls/up/download/head/rm`（up 支持 jsonl/json/csv/xlsx 多文件、BOM 坑；download 三格式整集下载）。
- gf-run：`run/runs/watch/cancel/rerun/export`、`rows/logs/qc`、`rmrun`。注明 watch 的 Ctrl+C 只退查看不取消、rows 缺省取第一个输出节点、qc --download 落 jsonl、rmrun --all 清空。

- [ ] **Step 5: 删除旧 reference.md，端到端示例留 gf-cli**

```bash
git rm .claude/skills/gf-cli/reference.md
```
（`scripts/build-pipeline.ps1` 保留在 gf-cli/scripts/，在 gf-cli SKILL.md 末尾「更多」里指向它。）

- [ ] **Step 6: 人工核对**

逐个打开 6 个 SKILL.md，核对：命令拼写与各 `commands/*.py` 的 `register` 一致；键名表与 `cmd_node_set` 实际分支一致；无残留旧 `{"pass":...}` 契约；每个 description 能让「该资源的操作」命中该技能。

- [ ] **Step 7: Commit**

```bash
cd "E:/代码/GraphFlow"
git add .claude/skills/ && git rm .claude/skills/gf-cli/reference.md
git commit -m "docs(gf): 技能按资源拆分（gf-cli 总入口 + workflow/node-prompt/model/dataset/run）"
```

---

## 完成开发

全部任务完成且各自测试通过后：
- 跑后端全量回归：`cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`，确认全绿。
- Announce: "I'm using the finishing-a-development-branch skill to complete this work."
- **REQUIRED SUB-SKILL:** Use superpowers:finishing-a-development-branch（验证测试 → 给选项 → 执行；用户惯例为本地合并 master 并删分支）。
