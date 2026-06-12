# RedLotus×GraphFlow 原生 Agent 跑数平台 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 RedLotus（pydantic-ai 三角色 Agent 框架）精简移植进 GraphFlow 后端，做成页面抽屉对话驱动、通过 `gf` CLI 操控工作流/模型/数据集的原生 Agent 跑数平台，含目标模式（goal_mode）自动续轮。

**Architecture:** 同进程 FastAPI：`backend/app/agent/` 模块（sandbox/tools/skills/factory/orchestrator/system/goal）+ `AgentSession`/`AgentMessage` 持久化 + `AgentTurnManager` 后台回合（仿 RunManager）+ 既有 `/api/events` SSE 通道扩展 payload。Agent 用 `gf` CLI（`GF_STATE_FILE` 按会话/Worker 隔离）以会话属主身份操作资源。前端全局右侧抽屉 `AgentDrawer`。

**Tech Stack:** pydantic-ai ≥1.80（OpenAIChatModel/FunctionToolset/TestModel/FunctionModel）、ddgs、pymupdf、python-docx；前端 antd 6 + react-markdown。

**Spec:** `docs/superpowers/specs/2026-06-11-redlotus-agent-design.md`（已获用户确认）。

---

## 执行约定（每个任务都适用）

- **分支**：全部工作在 `feature/agent` 分支（Task 1 创建），最后合并 master。
- **KISS（用户硬性约束）**：不预防未发生的 bug，不加投机抽象。移植时砍掉 RedLotus 的 lifecycle registry/hooks、ConversationLog、记忆注入、auto-compress、json_repair、ThinkingProvider——平台用 OpenAI 兼容直连即可。
- **提交格式**（PowerShell 5.1，不用 here-string、不用 `&&`）：
  ```powershell
  git add <files>; git commit -m "feat: 中文主题" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```
- **命令目录**：pytest/uv 在 `E:\代码\GraphFlow\backend` 下运行；npm/npx 在 `E:\代码\GraphFlow\frontend`；git 在仓库根。
- **测试命令**：`uv run pytest tests/<file> -q`（后端）、`npx vitest run`（前端）。
- 仓库根的 `项目设计.txt`、`.idea/`、`RedLotus/` 是用户本地文件，**不要 git add**（`RedLotus/` 在 Task 16 删除，本来就未跟踪）。

## pydantic-ai 1.80 关键 API（从 RedLotus 源码镜像，权威用法）

```python
from pydantic_ai import Agent, FunctionToolset, ModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import FunctionModel, AgentInfo
from pydantic_ai.messages import (ModelMessage, ModelMessagesTypeAdapter, ModelRequest,
    ModelResponse, PartDeltaEvent, TextPart, TextPartDelta, ToolCallPart, ToolReturnPart,
    UserPromptPart)

model = OpenAIChatModel("qwen-max", provider=OpenAIProvider(base_url=..., api_key=...),
                        settings=ModelSettings(temperature=0.3))
agent = Agent(model, toolsets=[FunctionToolset(tools, id="default")], instructions="系统提示")
result = await agent.run("用户输入", message_history=history, event_stream_handler=handler)
result.output                  # str
result.all_messages()          # list[ModelMessage]，含历史+本轮
ModelMessagesTypeAdapter.dump_json(msgs)      # bytes
ModelMessagesTypeAdapter.validate_json(text)  # list[ModelMessage]
```

- 工具 = 带 docstring 的（绑定）方法，pydantic-ai 从签名+docstring 生成 schema；`functools.wraps` 包一层不破坏 schema（RedLotus tool_telemetry 已验证）。
- **本计划所有工具一律 `async def`**（包装器假定协程，避免线程池里发 SSE）。
- `event_stream_handler` 签名 `async def handler(ctx, events)`，事件流里 `PartDeltaEvent.delta` 为 `TextPartDelta` 时取 `.content_delta`。
- pyyaml 经 `fastapi[standard]→uvicorn[standard]` 已可用，**不要**再加依赖。

## 与 spec 的实现取舍（已按 KISS 收束，执行时不要再改回去）

1. **cli.json 的 server 字段**取自会话创建请求的 `request.base_url`（dev 经 Vite 代理、prod 直连均可达），不做「启动时记录端口到 app.state」的机制——同一保证，更少机器。
2. **SSE kind 扩两个**：`message`（一条消息已落库，前端重拉）、`goal_round`（自动续轮轮次，前端徽标）。§8 的 delta/tool_start/tool_end/turn_done 不变。
3. run_command 的 timeout 上限 600s（默认 60）——`gf` 数据操作可能超 60s。
4. `mark_task_complete/mark_task_failed` 是 orchestrator 内部方法、不暴露为 Manager 工具（与 RedLotus 实际接线一致；Manager 工具 = create_todo_list + get_todo_list）。
5. Manager/Todo 状态是**回合批次内**的（goal 续轮共享同一 AgentSystem 实例）；跨用户消息不保留（持久化的只有 coordinator history_json）。

## 文件结构总览

```
backend/app/agent/
  __init__.py            # 空
  sandbox.py             # resolve_in(base, name) 路径沙盒
  subproc.py             # run_subprocess（杀进程树，RedLotus 原样移植）
  extract.py             # extract_text：pdf/docx/xlsx/txt/md/csv/json(l)/html
  skills.py              # SkillsManager + SkillsToolkit，根 = 仓库 .claude/skills/
  tools.py               # AgentToolkit（文件/run_command/search_web/extract）+ wrap_tools 遥测
  factory.py             # ModelConfig → OpenAIChatModel；create_agent
  prompts/__init__.py    # load_prompt + get_*_system_prompt
  prompts/*.md           # 8 个平台化提示词
  orchestrator.py        # TaskManager + 消息板 + WorkerOrchestrator（并行波次）
  goal.py                # REDLOTUS_GOAL 标记解析/剥离
  system.py              # AgentSystem：coordinator + 路由工具 + Manager 三阶段
  turns.py               # AgentTurnManager：后台回合、goal 循环、stop、重启恢复
backend/app/routers/agent.py   # /api/agent/* 六端点
backend/app/{models,events,config,main,cli}.py  # 增量修改
backend/tests/test_agent_*.py  # 任务内列明
frontend/src/agent/{parse.ts, parse.test.ts, AgentDrawer.tsx}
frontend/src/{App.tsx, api/events.ts, api/types.ts}  # 增量修改
```

---

### Task 1: 分支、依赖、路径沙盒与子进程原语

**Files:**
- Create: `backend/app/agent/__init__.py`、`backend/app/agent/sandbox.py`、`backend/app/agent/subproc.py`
- Modify: `backend/pyproject.toml`（uv add）
- Test: `backend/tests/test_agent_sandbox.py`

- [ ] **Step 1: 建分支 + 加依赖**

```powershell
git checkout -b feature/agent
cd backend; uv add "pydantic-ai>=1.80" ddgs pymupdf python-docx
```
预期：pyproject dependencies 增加 4 项，uv.lock 更新，无报错。

- [ ] **Step 2: 写失败测试**

`backend/tests/test_agent_sandbox.py`：
```python
import pytest

from app.agent.sandbox import resolve_in


def test_resolve_inside(tmp_path):
    assert resolve_in(tmp_path, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()


def test_resolve_dot_default(tmp_path):
    assert resolve_in(tmp_path, ".") == tmp_path.resolve()


def test_escape_dotdot(tmp_path):
    with pytest.raises(ValueError):
        resolve_in(tmp_path, "../outside.txt")


def test_escape_absolute(tmp_path):
    with pytest.raises(ValueError):
        resolve_in(tmp_path, "C:/Windows/win.ini")
```

- [ ] **Step 3: 跑测试确认失败**

`uv run pytest tests/test_agent_sandbox.py -q` → 预期 ModuleNotFoundError。

- [ ] **Step 4: 实现**

`backend/app/agent/__init__.py`：空文件。

`backend/app/agent/sandbox.py`：
```python
from pathlib import Path


def resolve_in(base: Path, name: str) -> Path:
    """把 name 解析到 base 目录内；绝对路径或 .. 逃逸则抛 ValueError。"""
    target = (base / name).resolve()
    if target != base.resolve() and not target.is_relative_to(base.resolve()):
        raise ValueError(f"路径越界: {name}")
    return target
```

`backend/app/agent/subproc.py`（RedLotus `subprocess_runner.py` 原样移植，仅删模块注释里对项目结构的引用）：
```python
"""可取消的子进程执行原语：run_command / execute_skill_script 共用杀进程树/超时/取消语义。"""
import asyncio
import os
import platform as _platform
import signal
import subprocess


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其后代（无 psutil 依赖），并收尸。"""
    if proc.returncode is not None:
        return
    try:
        if _platform.system() == "Windows":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


async def run_subprocess(
    args, *, shell: bool, cwd: str, env: dict | None = None, timeout: float
) -> tuple[str, str, int | None]:
    """跑子进程并返回 (stdout, stderr, returncode)；取消或超时都会杀掉整棵进程树。"""
    kwargs: dict = dict(
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env
    )
    if _platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    if shell:
        proc = await asyncio.create_subprocess_shell(args, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        raise subprocess.TimeoutExpired(args, timeout)
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        raise
    return (
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        proc.returncode,
    )
```

- [ ] **Step 5: 跑测试确认通过**

`uv run pytest tests/test_agent_sandbox.py -q` → 4 passed。

- [ ] **Step 6: 提交**

```powershell
git add backend/pyproject.toml backend/uv.lock backend/app/agent/ backend/tests/test_agent_sandbox.py
git commit -m "feat: Agent 模块骨架——依赖、路径沙盒、可取消子进程原语" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 技能系统（SkillsManager + SkillsToolkit）

**Files:**
- Create: `backend/app/agent/skills.py`
- Test: `backend/tests/test_agent_skills.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_skills.py`：
```python
import json

from app.agent.skills import SKILLS_DIR, SkillsManager, SkillsToolkit


def test_discovers_gf_cli_skill():
    sm = SkillsManager(SKILLS_DIR)
    assert "gf-cli" in [m.name for m in sm.get_all_metadata()]
    assert "gf-cli" in sm.get_skills_summary()


def test_load_instructions_and_resources():
    sm = SkillsManager(SKILLS_DIR)
    assert "gf" in sm.load_skill_instructions("gf-cli")
    assert "reference.md" in sm.list_skill_resources("gf-cli")
    assert sm.load_skill_resource("gf-cli", "reference.md")


def test_resource_escape_blocked():
    sm = SkillsManager(SKILLS_DIR)
    assert sm.load_skill_resource("gf-cli", "../../backend/app/config.py") is None


async def test_toolkit_unknown_skill(tmp_path):
    tk = SkillsToolkit(SkillsManager(SKILLS_DIR), tmp_path / "cli.json")
    assert "不存在" in await tk.get_skill_instructions("no-such-skill")


async def test_execute_script_unsupported_ext(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody", encoding="utf-8")
    (skill_dir / "x.exe").write_bytes(b"")
    tk = SkillsToolkit(SkillsManager(tmp_path / "skills"), tmp_path / "cli.json")
    assert "不支持" in await tk.execute_skill_script("demo", "x.exe")


async def test_execute_script_injects_gf_state(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody", encoding="utf-8")
    (skill_dir / "show_env.py").write_text(
        "import os; print(os.environ.get('GF_STATE_FILE', 'MISSING'))", encoding="utf-8")
    state = tmp_path / "cli.json"
    tk = SkillsToolkit(SkillsManager(tmp_path / "skills"), state)
    out = await tk.execute_skill_script("demo", "show_env.py")
    assert str(state) in out
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_skills.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/skills.py`**

```python
"""技能系统：扫描仓库 .claude/skills/<dir>/SKILL.md，供 Agent 现学现用（gf-cli 等）。"""
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.agent.subproc import run_subprocess

SKILLS_DIR = Path(__file__).resolve().parents[3] / ".claude" / "skills"
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SKILL_FILENAME = "SKILL.md"
BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass
class SkillMetadata:
    name: str
    description: str
    path: Path


@dataclass
class Skill:
    metadata: SkillMetadata
    instructions: str = ""
    resources: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def path(self) -> Path:
        return self.metadata.path


class SkillsManager:
    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, Skill] = {}
        self.refresh()

    def refresh(self) -> None:
        fresh: dict[str, Skill] = {}
        if self.skills_dir.exists():
            for item in self.skills_dir.iterdir():
                skill_file = item / SKILL_FILENAME
                if not (item.is_dir() and skill_file.is_file()):
                    continue
                parsed = self._parse(skill_file)
                if parsed:
                    fresh[parsed.name] = parsed
        self.skills = fresh

    def _parse(self, skill_file: Path) -> Skill | None:
        content = skill_file.read_text(encoding="utf-8")
        match = FRONTMATTER_PATTERN.match(content)
        if not match:
            return None
        front = yaml.safe_load(match.group(1))
        if not isinstance(front, dict) or not front.get("name"):
            return None
        meta = SkillMetadata(name=str(front["name"]), description=str(front.get("description", "")),
                             path=skill_file.parent)
        return Skill(metadata=meta, instructions=content[match.end():].strip())

    def get_all_metadata(self) -> list[SkillMetadata]:
        return [self.skills[n].metadata for n in sorted(self.skills)]

    def get_skills_summary(self) -> str:
        if not self.skills:
            return "当前没有可用的 Skills。"
        lines = ["## 可用的 Agent Skills", ""]
        lines += [f"- **{m.name}**: {m.description}" for m in self.get_all_metadata()]
        lines.append("")
        lines.append("用 `get_skill_instructions(skill_name)` 获取具体 Skill 的详细指令。")
        return "\n".join(lines)

    def load_skill_instructions(self, name: str) -> str | None:
        skill = self.skills.get(name)
        return skill.instructions if skill else None

    def list_skill_resources(self, skill_name: str) -> list[str]:
        skill = self.skills.get(skill_name)
        if not skill:
            return []
        return [str(p.relative_to(skill.path)) for p in skill.path.rglob("*")
                if p.is_file() and p.name != SKILL_FILENAME]

    def load_skill_resource(self, skill_name: str, resource_name: str) -> str | None:
        skill = self.skills.get(skill_name)
        if not skill:
            return None
        target = self._resolve_within(skill.path, resource_name)
        if target is None or not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _resolve_within(skill_dir: Path, relative: str) -> Path | None:
        base = skill_dir.resolve()
        try:
            target = (base / relative).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(base) else None


class SkillsToolkit:
    """技能工具集；execute_skill_script 注入会话 GF_STATE_FILE，脚本里可直接用 gf。"""

    def __init__(self, manager: SkillsManager, state_file: Path):
        self._manager = manager
        self._state_file = state_file

    async def list_available_skills(self) -> str:
        """列出所有可用的 Agent Skills（名称与描述）。执行复杂任务前先看有哪些技能可用。"""
        metas = self._manager.get_all_metadata()
        if not metas:
            return "当前没有可用的 Skills。"
        lines = [f"{i}. {m.name} —— {m.description}" for i, m in enumerate(metas, 1)]
        lines.append("用 get_skill_instructions(skill_name) 获取详细指令。")
        return "\n".join(lines)

    async def get_skill_instructions(self, skill_name: str) -> str:
        """获取指定 Skill 的完整指令（工作流程、键名表、示例）。使用技能前的必要步骤。
        Parameters:
            skill_name: Skill 名称（如 "gf-cli"）
        """
        instructions = self._manager.load_skill_instructions(skill_name)
        if instructions is None:
            names = ", ".join(m.name for m in self._manager.get_all_metadata()) or "无"
            return f"错误: Skill '{skill_name}' 不存在。可用: {names}"
        resources = self._manager.list_skill_resources(skill_name)
        out = [f"# Skill: {skill_name}", instructions]
        if resources:
            out.append("可用的额外资源: " + ", ".join(resources)
                       + "\n用 load_skill_resource(skill_name, resource_name) 加载。")
        return "\n\n".join(out)

    async def load_skill_resource(self, skill_name: str, resource_name: str) -> str:
        """加载 Skill 的额外资源文件（参考文档、脚本等），按需加载避免一次塞满上下文。
        Parameters:
            skill_name: Skill 名称
            resource_name: 资源文件名（如 "reference.md", "scripts/x.ps1"）
        """
        content = self._manager.load_skill_resource(skill_name, resource_name)
        if content is None:
            return f"错误: 资源 '{resource_name}' 不存在或越界"
        return content

    async def refresh_skills(self) -> str:
        """重新扫描技能目录，发现新增或更新的 Skills。"""
        self._manager.refresh()
        return f"Skills 已刷新，当前 {len(self._manager.skills)} 个可用。"

    async def execute_skill_script(self, skill_name: str, script_name: str,
                                   args: str = "", timeout: float = 300) -> str:
        """执行 Skill 内的脚本（.py/.ps1/.bat/.sh），返回输出；脚本代码本身不进上下文。
        Parameters:
            skill_name: Skill 名称
            script_name: 脚本相对路径（仅限技能目录内）
            args: 传给脚本的参数
            timeout: 最长执行秒数，默认 300
        """
        skill = self._manager.skills.get(skill_name)
        if not skill:
            return f"错误: Skill '{skill_name}' 不存在"
        script = SkillsManager._resolve_within(skill.path, script_name)
        if script is None or not script.exists():
            return f"错误: 脚本 '{script_name}' 不存在或越界"
        executors = {".py": [sys.executable], ".sh": ["bash"],
                     ".bat": ["cmd", "/c"], ".ps1": ["powershell", "-File"]}
        ext = script.suffix.lower()
        if ext not in executors:
            return f"错误: 不支持的脚本类型 '{ext}'"
        cmd = executors[ext] + [str(script)] + (args.split() if args else [])
        # PYTHONIOENCODING：Windows 管道默认 cp936，子进程打印中文会乱码
        env = {**os.environ, "GF_STATE_FILE": str(self._state_file),
               "PYTHONPATH": str(BACKEND_DIR), "PYTHONIOENCODING": "utf-8"}
        import subprocess
        try:
            stdout, stderr, code = await run_subprocess(
                cmd, shell=False, cwd=str(skill.path), env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"错误: 脚本执行超时（{timeout} 秒）"
        output = stdout + stderr
        return f"返回码: {code}\n输出:\n{output}" if output else f"执行完成，返回码: {code}"

    @property
    def tools(self) -> list:
        return [self.list_available_skills, self.get_skill_instructions,
                self.load_skill_resource, self.refresh_skills, self.execute_skill_script]
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_skills.py -q` → 6 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/skills.py backend/tests/test_agent_skills.py
git commit -m "feat: Agent 技能系统——根目录指向仓库 .claude/skills，脚本执行注入 GF_STATE_FILE" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 文档解析 extract.py

**Files:**
- Create: `backend/app/agent/extract.py`
- Test: `backend/tests/test_agent_extract.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_extract.py`：
```python
from app.agent.extract import extract_text


def test_txt(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n\n\n\nworld", encoding="utf-8")
    assert extract_text(f) == "hello\nworld"


def test_csv(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("q,a\n1,2", encoding="utf-8")
    assert "q,a" in extract_text(f)


def test_missing(tmp_path):
    assert "不存在" in extract_text(tmp_path / "nope.txt")


def test_unsupported(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x00")
    assert "不支持" in extract_text(f)


def test_html(tmp_path):
    f = tmp_path / "a.html"
    f.write_text("<html><script>x()</script><p>正文</p></html>", encoding="utf-8")
    out = extract_text(f)
    assert "正文" in out and "x()" not in out
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_extract.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/extract.py`**（RedLotus ExtractFileContent.py 移植，路径解析交给调用方，重依赖懒加载）

```python
"""文档文本提取：pdf/docx/xlsx/txt/md/csv/json(l)/html → 纯文本。路径由调用方解析。"""
import re
from html.parser import HTMLParser
from pathlib import Path

_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "div", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "p", "section", "tr"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._ignored_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(c for c in self._chunks if c.strip())


def _clean(content: str) -> str:
    content = re.sub(r"[ \t]{2,}", " ", content)
    content = re.sub(r"^[ \t]+|[ \t]+$", "", content, flags=re.MULTILINE)
    content = re.sub(r"\n{2,}", "\n", content).strip("\n")
    return content.strip()


def _from_pdf(fp: Path) -> str:
    import fitz
    with fitz.open(fp) as pdf:
        return "\n".join(page.get_text() for page in pdf)


def _from_excel(fp: Path) -> str:
    import pandas as pd
    df = pd.read_excel(fp)
    return "\n\n".join(f"{col}:\n{df[col].to_string()}" for col in df.columns)


def _from_docx(fp: Path) -> str:
    import docx
    return "\n".join(p.text for p in docx.Document(str(fp)).paragraphs)


def _from_html(fp: Path) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(fp.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return parser.text()


def extract_text(file_path: Path) -> str:
    if not file_path.exists():
        return f"Error: 文件不存在: {file_path.name}"
    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            content = _from_pdf(file_path)
        elif ext in (".xlsx", ".xls"):
            content = _from_excel(file_path)
        elif ext == ".docx":
            content = _from_docx(file_path)
        elif ext in (".txt", ".md", ".markdown", ".csv", ".json", ".jsonl"):
            content = file_path.read_text(encoding="utf-8", errors="replace")
        elif ext in (".html", ".htm"):
            content = _from_html(file_path)
        else:
            return f"Error: 不支持的文件类型 '{ext}'"
    except Exception as e:
        return f"Error: 提取失败: {e}"
    content = _clean(content)
    return content if content else "文件为空"
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_extract.py -q` → 5 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/extract.py backend/tests/test_agent_extract.py
git commit -m "feat: Agent 文档解析 extract_text（pdf/docx/xlsx/文本/html）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: AgentToolkit 与工具遥测包装

**Files:**
- Create: `backend/app/agent/tools.py`
- Test: `backend/tests/test_agent_tools.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_tools.py`：
```python
import json
import sys

import pytest

from app.agent.tools import EMIT, ROLE, AgentToolkit, wrap_tools


@pytest.fixture
def tk(tmp_path):
    return AgentToolkit(tmp_path, tmp_path / "cli.json", confirm_delete=False)


async def test_write_read_list(tk, tmp_path):
    assert "已写入" in await tk.write_file("sub/a.txt", "你好")
    assert await tk.read_file("sub/a.txt") == "你好"
    assert "sub/" in await tk.list_directory()
    assert "a.txt" in await tk.list_directory("sub")


async def test_path_escape_blocked(tk):
    assert "Security error" in await tk.read_file("../secret.txt")
    assert "Security error" in await tk.write_file("C:/x.txt", "x")


async def test_dangerous_command_blocked(tk):
    assert "Security error" in await tk.run_command("echo hi | sh")
    assert "Security error" in await tk.run_command("eval something")
    assert "Security error" in await tk.run_command("nohup python x.py &")


async def test_run_command_echo(tk):
    out = await tk.run_command("echo hola")
    assert "hola" in out and "Return code: 0" in out


async def test_gf_delete_intercepted(tk):
    out = await tk.run_command("gf data rm 种子集")
    assert "需用户确认" in out


async def test_gf_delete_allowed_with_confirm(tmp_path):
    tk = AgentToolkit(tmp_path, tmp_path / "cli.json", confirm_delete=True)
    out = await tk.run_command("gf data rm 种子集")
    # 改写为 python -m app.cli 真实执行：无状态文件 → gf 报「未登录」而非被拦截
    assert "需用户确认" not in out
    assert "未登录" in out


async def test_gf_node_rm_not_intercepted(tk):
    out = await tk.run_command("gf node rm input_1")
    assert "需用户确认" not in out


async def test_gf_rewrite_uses_state_env(tmp_path):
    state = tmp_path / "cli.json"
    state.write_text(json.dumps({"server": "http://127.0.0.1:1", "cookie": "x"}), encoding="utf-8")
    tk = AgentToolkit(tmp_path, state, confirm_delete=False)
    out = await tk.run_command("gf st", timeout=30)
    # cookie 无效/服务不可达都行——关键是 gf 真的跑起来并读到了 GF_STATE_FILE（未报「未登录」）
    assert "未登录" not in out


async def test_truncation_via_wrapper(tmp_path):
    async def big() -> str:
        """返回大文本。"""
        return "x" * 30000

    wrapped = wrap_tools([big])[0]
    out = await wrapped()
    assert len(out) < 25000 and "截断" in out


async def test_wrapper_emits_events(tmp_path):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def ping(text: str) -> str:
        """回声。
        Parameters:
            text: 文本
        """
        return f"pong:{text}"

    token_e = EMIT.set(emit)
    token_r = ROLE.set("worker_1")
    try:
        wrapped = wrap_tools([ping])[0]
        assert await wrapped(text="a") == "pong:a"
    finally:
        EMIT.reset(token_e)
        ROLE.reset(token_r)
    kinds = [k for k, _ in events]
    assert kinds == ["tool_start", "tool_end"]
    assert events[1][1]["status"] == "ok"
    assert events[1][1]["agent_role"] == "worker_1"
    assert events[0][1]["tool"] == "ping"
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_tools.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/tools.py`**

```python
"""Agent 基础工具集 + 遥测包装。所有工具均为 async def（包装器在事件循环内发 SSE/落库）。"""
import asyncio
import functools
import os
import re
import subprocess
import sys
from contextvars import ContextVar
from pathlib import Path

from ddgs import DDGS

from app.agent.extract import extract_text
from app.agent.sandbox import resolve_in

MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_COMMAND_TIMEOUT = 600
BACKEND_DIR = Path(__file__).resolve().parents[2]

# 回合上下文：TurnManager 设 EMIT（async (kind, data) -> None），各角色运行前设 ROLE
EMIT: ContextVar = ContextVar("agent_emit", default=None)
ROLE: ContextVar[str] = ContextVar("agent_role", default="coordinator")

DANGEROUS_PATTERNS = ["rm -rf /", "rm -rf /*", "mkfs.", "dd if=", ":(){:|:&};:",
                      "> /dev/sda", "chmod -R 777 /", "| sh", "| bash"]
DANGEROUS_START_PATTERNS = ["eval ", "exec "]
GF_DELETE_RE = re.compile(r"gf\s+(wf|data|model)\s+rm\b")
BACKGROUND_RE = re.compile(r"\b(start|nohup|setsid)\b|&\s*$", re.IGNORECASE)


def truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    omitted = len(text) - MAX_TOOL_OUTPUT_CHARS
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n\n[输出已截断，省略 {omitted} 字符]"


def _brief(kwargs: dict) -> str:
    if "command" in kwargs:
        return str(kwargs["command"])[:80]
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(kwargs.items())[:3])[:80]


def wrap_tools(tools: list) -> list:
    """给每个 async 工具套壳：tool_start/tool_end 事件 + 字符串结果截断。"""
    return [_wrap(t) for t in tools]


def _wrap(fn):
    name = getattr(fn, "__name__", "tool")

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        emit = EMIT.get()
        data = {"tool": name, "args_brief": _brief(kwargs), "agent_role": ROLE.get()}
        if emit:
            await emit("tool_start", data)
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            if emit:
                await emit("tool_end", {**data, "status": "error", "output_brief": str(e)[:200]})
            raise
        if isinstance(result, str):
            result = truncate(result)
        if emit:
            brief = (result if isinstance(result, str) else repr(result))[:200]
            await emit("tool_end", {**data, "status": "ok", "output_brief": brief})
        return result

    return wrapper


class AgentToolkit:
    """会话级基础工具：文件读写限会话工作目录，run_command 注入 GF_STATE_FILE。"""

    def __init__(self, workdir: Path, state_file: Path, confirm_delete: bool):
        self._workdir = Path(workdir)
        self._state_file = Path(state_file)
        self._confirm_delete = confirm_delete

    async def read_file(self, path: str) -> str:
        """读取会话工作目录内的文件内容。
        Parameters:
            path: 文件路径，相对工作目录
        """
        try:
            content = resolve_in(self._workdir, path).read_text(encoding="utf-8", errors="replace")
            return content if content else "文件为空"
        except ValueError as e:
            return f"Security error: {e}"
        except OSError as e:
            return f"Error: {e}"

    async def write_file(self, path: str, content: str) -> str:
        """写入（覆盖）会话工作目录内的文件，自动创建父目录。
        Parameters:
            path: 文件路径，相对工作目录
            content: 文件内容
        """
        try:
            fp = resolve_in(self._workdir, path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"已写入 {path}（{len(content)} 字符）"
        except ValueError as e:
            return f"Security error: {e}"
        except OSError as e:
            return f"Error: {e}"

    async def list_directory(self, path: str = "") -> str:
        """列出会话工作目录（或其子目录）下的条目，目录以 / 结尾。
        Parameters:
            path: 可选子目录，相对工作目录
        """
        try:
            target = resolve_in(self._workdir, path or ".")
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
            return "\n".join(entries) if entries else "目录为空"
        except ValueError as e:
            return f"Security error: {e}"
        except OSError as e:
            return f"Error: {e}"

    async def run_command(self, command: str, timeout: int = 60) -> str:
        """执行终端命令（cwd=会话工作目录）。操作 GraphFlow 用 gf 命令（先学 gf-cli 技能）。
        Parameters:
            command: 要执行的命令
            timeout: 超时秒数，默认 60，最大 600
        """
        cmd = command.strip()
        lower = cmd.lower()
        for p in DANGEROUS_PATTERNS:
            if p in lower:
                return f"Security error: 检测到危险命令模式 '{p}'"
        for p in DANGEROUS_START_PATTERNS:
            if lower.startswith(p):
                return f"Security error: 检测到危险命令模式 '{p}'"
        if BACKGROUND_RE.search(cmd):
            return "Security error: 不允许后台进程"
        if GF_DELETE_RE.search(cmd) and not self._confirm_delete:
            return ("删除操作需用户确认：请向用户说明将要删除的资源，"
                    "在回复末尾单独一行输出 [confirm_delete] <完整 gf 命令>，然后结束回合等待确认。")
        if cmd == "gf" or cmd.startswith("gf "):
            cmd = f'"{sys.executable}" -m app.cli{cmd[2:]}'
        # PYTHONIOENCODING：Windows 管道默认 cp936，gf 打印中文会乱码
        env = {**os.environ, "GF_STATE_FILE": str(self._state_file),
               "PYTHONPATH": str(BACKEND_DIR), "PYTHONIOENCODING": "utf-8"}
        timeout = max(1, min(int(timeout), MAX_COMMAND_TIMEOUT))
        from app.agent.subproc import run_subprocess
        try:
            stdout, stderr, code = await run_subprocess(
                cmd, shell=True, cwd=str(self._workdir), env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"Error: 命令执行超时（{timeout} 秒）"
        output = stdout + stderr
        return f"Return code: {code}\nOutput:\n{output}" if output else f"执行完成，Return code: {code}"

    async def search_web(self, query: str, max_results: int = 5) -> str:
        """联网搜索，返回标题/链接/摘要列表。
        Parameters:
            query: 搜索关键词
            max_results: 最多返回条数，默认 5
        """
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results, region="cn-zh"))

        try:
            results = await asyncio.to_thread(_search)
        except Exception as e:
            return f"搜索出错: {e}"
        if not results:
            return "没有找到相关结果。"
        return "\n".join(
            f"{i}. {r.get('title', '')}\n   链接: {r.get('href', '')}\n   摘要: {r.get('body', '')}"
            for i, r in enumerate(results, 1))

    async def extract_file_content(self, path: str) -> str:
        """提取文档文本（PDF/docx/xlsx/txt/md/csv/json/html），适合读导出数据和参考资料。
        Parameters:
            path: 文件路径，相对会话工作目录
        """
        try:
            fp = resolve_in(self._workdir, path)
        except ValueError as e:
            return f"Security error: {e}"
        return await asyncio.to_thread(extract_text, fp)

    @property
    def tools(self) -> list:
        return [self.read_file, self.write_file, self.list_directory,
                self.run_command, self.search_web, self.extract_file_content]
```

注意：`echo hola` 这类无元字符命令也走 `shell=True`（Windows 下 cmd 解析），与 RedLotus 的 Windows 分支一致。

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_tools.py -q` → 11 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/tools.py backend/tests/test_agent_tools.py
git commit -m "feat: AgentToolkit——沙盒文件工具、run_command（gf 改写+GF_STATE_FILE+删除硬拦）、搜索与遥测包装" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: factory.py——ModelConfig → pydantic-ai

**Files:**
- Create: `backend/app/agent/factory.py`
- Test: `backend/tests/test_agent_factory.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_factory.py`：
```python
from pydantic_ai.models.test import TestModel

from app import crypto
from app.agent import factory
from app.models import ModelConfig


def _mc(**over):
    base = dict(user_id=1, name="m1", model_name="qwen-max", base_url="http://llm.local/v1",
                api_key_enc=crypto.encrypt("sk-test"),
                default_params_json='{"temperature": 0.3, "max_tokens": 100, "json_mode": true}')
    base.update(over)
    return ModelConfig(**base)


def test_create_model_decrypts_key(monkeypatch):
    captured = {}
    real = factory.OpenAIProvider

    def spy(base_url, api_key):
        captured.update(base_url=base_url, api_key=api_key)
        return real(base_url=base_url, api_key=api_key)

    monkeypatch.setattr(factory, "OpenAIProvider", spy)
    model = factory.create_model(_mc())
    assert captured == {"base_url": "http://llm.local/v1", "api_key": "sk-test"}
    assert model.model_name == "qwen-max"
    assert model.settings["temperature"] == 0.3
    assert model.settings["max_tokens"] == 100
    assert "json_mode" not in model.settings  # 非 ModelSettings 键被忽略


def test_create_model_no_key():
    model = factory.create_model(_mc(api_key_enc="", default_params_json="{}"))
    assert model.model_name == "qwen-max"
    assert model.settings is None


async def test_create_agent_runs_tools():
    async def ping(text: str) -> str:
        """回声工具。
        Parameters:
            text: 文本
        """
        return f"pong:{text}"

    agent = factory.create_agent(TestModel(), [ping], "你是测试")
    result = await agent.run("hi")
    assert "pong:" in str(result.output)
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_factory.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/factory.py`**

```python
"""从 GraphFlow ModelConfig 构造 pydantic-ai Agent（OpenAI 兼容直连，api_key 现解密现用）。"""
import json

from pydantic_ai import Agent, FunctionToolset, ModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app import crypto
from app.agent.tools import wrap_tools
from app.models import ModelConfig

SETTINGS_KEYS = ("temperature", "top_p", "max_tokens", "timeout")


def create_model(mc: ModelConfig) -> OpenAIChatModel:
    params = json.loads(mc.default_params_json)
    kw = {k: params[k] for k in SETTINGS_KEYS if params.get(k) is not None}
    provider = OpenAIProvider(
        base_url=mc.base_url,
        api_key=crypto.decrypt(mc.api_key_enc) if mc.api_key_enc else "none")
    return OpenAIChatModel(mc.model_name, provider=provider,
                           settings=ModelSettings(**kw) if kw else None)


def create_agent(model, tools: list, instructions: str) -> Agent:
    """model 可传 ModelConfig（按配置构造）或现成 Model 实例（测试用 TestModel/FunctionModel）。"""
    if isinstance(model, ModelConfig):
        model = create_model(model)
    return Agent(model, toolsets=[FunctionToolset(wrap_tools(tools), id="default")],
                 instructions=instructions)
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_factory.py -q` → 3 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/factory.py backend/tests/test_agent_factory.py
git commit -m "feat: Agent factory——ModelConfig 构造 OpenAI 兼容模型与带遥测工具集的 Agent" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 平台化提示词

**Files:**
- Create: `backend/app/agent/prompts/__init__.py` 及 8 个 `.md`
- Test: `backend/tests/test_agent_prompts.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_prompts.py`：
```python
import re

from app.agent.prompts import (get_coordinator_system_prompt, get_manager_system_prompt,
                               get_worker_system_prompt, load_prompt)
from app.agent.skills import SKILLS_DIR, SkillsManager


def test_render_all_roles():
    sm = SkillsManager(SKILLS_DIR)
    for text in (get_coordinator_system_prompt(sm), get_manager_system_prompt(sm),
                 get_worker_system_prompt(sm), get_worker_system_prompt(sm, parallel=True)):
        assert "gf-cli" in text                      # 技能摘要已注入
        assert not re.search(r"\{[a-z_]+\}", text)   # 无未替换占位符


def test_coordinator_has_goal_and_delete_rules():
    sm = SkillsManager(SKILLS_DIR)
    text = get_coordinator_system_prompt(sm)
    assert "REDLOTUS_GOAL:CONTINUE" in text and "REDLOTUS_GOAL:DONE" in text
    assert "[confirm_delete]" in text


def test_worker_has_report_protocol_and_state_note():
    sm = SkillsManager(SKILLS_DIR)
    text = get_worker_system_prompt(sm, parallel=True)
    assert "SUCCESS:" in text and "FAILED:" in text
    assert "gf use" in text
    assert "report_progress" in text


def test_templates_loadable():
    for name in ("manager_planning_new.md", "manager_planning_continue.md", "manager_summary.md"):
        assert "{user_input}" in load_prompt(name)
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_prompts.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 写 8 个 md 文件**（注意：除 `{current_time}`/`{skills_summary}`/`{common_conduct}`/`{user_input}`/`{current_todo}`/`{final_summary}` 占位符外，**正文不得出现裸花括号**，`.format()` 会炸）

`backend/app/agent/prompts/common_conduct.md`：
```markdown
## 行为约束

### 最小复杂度
- 不做超出当前需求的事；不预防未发生的问题；三行相似代码好过过早抽象。

### 删除纪律（GraphFlow 资源）
- 删除工作流/数据集/模型配置（gf wf rm / gf data rm / gf model rm）前必须停下：在回复末尾单独一行输出
  `[confirm_delete] <完整 gf 命令>`
  然后结束回合等待用户确认。系统会渲染成确认按钮；用户确认后你会收到以「确认」开头的消息，此时方可执行。
- 未经确认直接执行删除会被系统硬拦截。
- `gf node rm`（节点可重建）不需要确认。

### 数据真实性
- 不编造数据或结果；真实数据拿不到就如实报告。

### 工作目录
- 文件读写、`gf export -o` 的输出都只在会话工作目录内（用相对路径）。

### 语言与输出
- 用用户的语言回复，默认中文；只输出必要结论，不复述请求、不加免责声明。
```

`backend/app/agent/prompts/coordinator_system.md`：
```markdown
你是「红莲」，GraphFlow 数据合成平台的内置 Agent。用户在平台上维护工作流（节点画布）、数据集与模型配置；你通过 `gf` 命令行（run_command 工具）替用户操作这些资源，前端画布会实时联动。
Current Time: {current_time}

## 平台与 gf CLI

- 所有 GraphFlow 资源操作都通过 run_command 执行 `gf` 命令完成（工作流/节点/连线/模型/数据集/运行/导出）。
- **首次操作 GraphFlow 资源前，必须先 get_skill_instructions("gf-cli") 学习命令、键名表与 op 语法**，不要凭猜测拼命令。
- 你的工作目录是本会话专属目录，文件读写与 gf 导出都发生在这里。

{skills_summary}

## 任务路由

对每个用户请求，三选一：
1. **直接解决**——单个低风险操作一短串工具调用能完成（查看资源、回答问题、一两条 gf 命令）。
2. **派给 Worker**（execute_task_with_worker）——单个自包含任务（搭一条完整链路并跑通、写脚本处理数据文件）。
3. **派给 Manager**（execute_task_with_manager）——需要规划分解、多子任务并行的复杂任务；用户在上一次 Manager 结果上迭代时传 continue_from_previous=True。

拿不准 1/2 时选 2，拿不准 2/3 时选 3。

## 目标模式（平台特色）

当用户给出需要多轮推进的**目标**（例如「把这条流水线调到首轮质检通过率 90% 以上」「把这批数据清洗成 alpaca 格式」）时，进入目标循环：行动 → 对照目标检验 → 报告进展与调整 → 续轮。由你根据目标自行制定验证方式。
- 数据质量评估优先用规则（行数/格式/字段齐全：gf export 到工作目录后 read_file 检查）；语义质量用 LLM 自评且抽样不超过 20 条控制成本。
- **每轮回复末尾必须输出标记**：目标未达成且应继续时输出 `<!-- REDLOTUS_GOAL:CONTINUE -->`；目标达成或需要用户决策时输出 `<!-- REDLOTUS_GOAL:DONE -->`。系统看到 CONTINUE 会自动以「继续推进目标」开启下一轮。
- 普通问答/单次任务**不要**输出任何标记。

{common_conduct}

## 执行后

完成后简要汇报结果即可；不要追加第二次派发去“改进”结果，也不要问用户是否满意。失败时直接说明原因。需要用户澄清时，在回复中提问并结束回合（没有 ask_user 工具）。
```

`backend/app/agent/prompts/manager_system.md`：
```markdown
你是「红莲」的 Manager（规划者），运行在 GraphFlow 数据合成平台内。
Current Time: {current_time}

{skills_summary}

## 你的角色

你只负责把复杂请求拆成任务清单（create_todo_list），系统会派发 Worker 并行执行；你自己不执行任何操作。

## 规划原则

1. 拆成原子、单目标的子任务。
2. 最大化并行：不真正依赖彼此产出的任务不要加依赖。
3. 只在任务 B 确实需要任务 A 的产出时声明依赖。
4. 任务描述自包含：Worker 看不到对话历史，描述里写清目标工作流名、节点名、列名等全部上下文。
5. 涉及 GraphFlow 操作的任务，在描述中提醒 Worker 先 get_skill_instructions("gf-cli")。

## 流程

1. 分析请求设计任务清单（想清楚什么能并行）。
2. 调 create_todo_list 创建。
3. 系统执行后返回执行报告。
4. 基于报告产出**直接回答用户问题**的最终报告，不要罗列“做了什么”。

{common_conduct}
```

`backend/app/agent/prompts/worker_system.md`：
```markdown
你是「红莲」的 Worker（执行者），运行在 GraphFlow 数据合成平台内。
Current Time: {current_time}

{skills_summary}

## GraphFlow 操作

- 通过 run_command 执行 `gf` 命令操作工作流/节点/模型/数据集/运行/导出。
- **首次操作前先 get_skill_instructions("gf-cli")**，按技能里的键名表与 op 语法拼命令。
- **你持有独立的 gf 状态文件**：先 `gf use <目标工作流>` 再做节点操作，不会影响其他 Worker。
- 处理数据文件时优先写 Python 脚本到工作目录再 run_command 执行，而不是连环单步工具调用。

## 汇报格式（机器解析）

最终回复第一行必须以下列前缀之一开头（系统按字面匹配）：
- `SUCCESS:` 后接一句话总结完成了什么
- `FAILED:` 后接一句话失败原因

之后可附细节（产物路径、关键数据、建议），保持简洁。

{common_conduct}
```

`backend/app/agent/prompts/worker_parallel_addon.md`：
```markdown

## 并行协作

你是并行执行的多个 Worker 之一，拥有两个协作工具：
- check_other_workers_progress()：查看其他 Worker 的进展与结果
- report_progress(message)：向其他 Worker 通报你的进展

当你的任务可能与他人产出相关、或完成关键里程碑时使用。
```

`backend/app/agent/prompts/manager_planning_new.md`：
```markdown
请分析以下用户请求并创建任务清单（Todo List）。
用户请求：{user_input}

用 create_todo_list 工具生成任务清单。

并行规则（重要）：
- 互不依赖的任务会被多个 Worker 同时并行执行
- 只在任务确实需要另一任务的产出时声明依赖
- 每个任务描述要详细到 Worker 能独立完成
```

`backend/app/agent/prompts/manager_planning_continue.md`：
```markdown
用户对上一轮结果提出了新要求或反馈。

当前任务清单状态：
{current_todo}

用户的新要求/反馈：{user_input}

请创建更新后的任务清单来满足新要求。互不依赖的任务会并行执行，只在真正需要时声明依赖。
```

`backend/app/agent/prompts/manager_summary.md`：
```markdown
任务执行完毕。请基于下面的执行报告，直接回答用户最初的问题。

用户最初的问题：{user_input}

执行报告：
{final_summary}

要求：
- 不要汇报执行状态（如「任务完成」「文件已创建」）
- 像对话一样直接回答用户的问题，提取报告中的关键信息
- 如果任务失败导致无法回答，简要说明原因
```

- [ ] **Step 4: 实现 `backend/app/agent/prompts/__init__.py`**

```python
"""提示词加载与渲染。模板占位符：current_time / skills_summary / common_conduct。"""
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _render(name: str, skills_manager) -> str:
    return load_prompt(name).format(
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        skills_summary=skills_manager.get_skills_summary(),
        common_conduct=load_prompt("common_conduct.md"),
    )


def get_coordinator_system_prompt(skills_manager) -> str:
    return _render("coordinator_system.md", skills_manager)


def get_manager_system_prompt(skills_manager) -> str:
    return _render("manager_system.md", skills_manager)


def get_worker_system_prompt(skills_manager, parallel: bool = False) -> str:
    text = _render("worker_system.md", skills_manager)
    return text + load_prompt("worker_parallel_addon.md") if parallel else text
```

- [ ] **Step 5: 跑测试确认通过**

`uv run pytest tests/test_agent_prompts.py -q` → 4 passed。

- [ ] **Step 6: 提交**

```powershell
git add backend/app/agent/prompts/ backend/tests/test_agent_prompts.py
git commit -m "feat: Agent 三角色平台化提示词——gf 技能强制流、删除纪律、目标模式标记协议" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: orchestrator.py——TaskManager 与 Worker 并行波次

**Files:**
- Create: `backend/app/agent/orchestrator.py`
- Test: `backend/tests/test_agent_orchestrator.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_orchestrator.py`：
```python
import json

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from app.agent.orchestrator import Task, TaskManager, TaskStatus, WorkerOrchestrator
from app.agent.skills import SKILLS_DIR, SkillsManager
from app.agent.tools import AgentToolkit


async def test_create_todo_validates():
    tm = TaskManager()
    assert "解析失败" in await tm.create_todo_list("not json")
    assert "重复" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a"}, {"id": "1", "description": "b"}]))
    assert "未知依赖" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a", "dependencies": ["9"]}]))
    assert "循环依赖" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a", "dependencies": ["2"]},
         {"id": "2", "description": "b", "dependencies": ["1"]}]))
    out = await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "搜 A"}, {"id": "2", "description": "搜 B"},
         {"id": "3", "description": "写报告", "dependencies": ["1", "2"]}]))
    assert "搜 A" in out and len(tm.tasks) == 3


def test_ready_waves_and_retry():
    tm = TaskManager()
    for tid, deps in (("1", []), ("2", []), ("3", ["1", "2"])):
        tm.tasks[tid] = Task(id=tid, description=f"t{tid}", dependencies=deps)
        tm.task_order.append(tid)
    assert [t.id for t in tm.get_all_ready_tasks()] == ["1", "2"]
    tm.mark_task_complete("1", "r1")
    assert [t.id for t in tm.get_all_ready_tasks()] == ["2"]
    tm.mark_task_failed("2", "boom")
    assert tm.tasks["2"].status is TaskStatus.PENDING  # 还有重试机会
    for _ in range(3):
        tm.mark_task_failed("2", "boom")
    assert tm.tasks["2"].status is TaskStatus.FAILED
    assert tm.has_failed_tasks() and not tm.is_all_completed()


def _worker_model(reply: str):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(reply)])
    return FunctionModel(fn)


def _orch(tmp_path, model):
    sm = SkillsManager(SKILLS_DIR)
    tm = TaskManager()

    def make_tools(state_file):
        return AgentToolkit(tmp_path, state_file, confirm_delete=False).tools

    return tm, WorkerOrchestrator(task_manager=tm, worker_model=model, workdir=tmp_path,
                                  make_tools=make_tools, skills_manager=sm)


async def test_adhoc_worker_success(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("SUCCESS: 完成了"))
    ok, out = await orch.execute_task_with_worker("做点事", user_goal="目标")
    assert ok and out.startswith("SUCCESS:")
    assert (tmp_path / "worker_adhoc_1_cli.json").exists()  # 独立 gf 状态副本


async def test_parallel_waves_complete(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("SUCCESS: done"))
    await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a"}, {"id": "2", "description": "b"},
         {"id": "3", "description": "c", "dependencies": ["1", "2"]}]))
    summary = await orch.execute_all_tasks_parallel("总目标")
    assert tm.is_all_completed()
    assert "3/3" in summary
    assert (tmp_path / "worker_1_cli.json").exists()
    assert (tmp_path / "worker_2_cli.json").exists()


async def test_failed_worker_marks_failed(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("FAILED: 不行"))
    await tm.create_todo_list(json.dumps([{"id": "1", "description": "a"}]))
    summary = await orch.execute_all_tasks_parallel("总目标")
    assert tm.has_failed_tasks()
    assert "失败" in summary
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_orchestrator.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/orchestrator.py`**

```python
"""任务清单 + Worker 并行波次编排（RedLotus WorkerOrchestrator 精简移植）。"""
import asyncio
import json
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.agent.factory import create_agent
from app.agent.prompts import get_worker_system_prompt
from app.agent.tools import ROLE

MAX_WORKER_CONCURRENT = 3
MAX_WAVES = 15


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    retry_count: int = 0
    max_retries: int = 3
    dependencies: list[str] = field(default_factory=list)
    failure_history: list[str] = field(default_factory=list)
    history: list = field(default_factory=list)  # Worker 的 ModelMessage 历史，跨重试沿用


class TaskManager:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.task_order: list[str] = []

    async def create_todo_list(self, tasks_json: str) -> str:
        """从 JSON 创建任务清单（覆盖旧清单）。
        Parameters:
            tasks_json: JSON 数组，格式 [{"id": "1", "description": "任务描述", "dependencies": ["依赖的任务 id"]}]
        """
        try:
            data = json.loads(tasks_json)
        except json.JSONDecodeError as e:
            return f"错误: JSON 解析失败 - {e}"
        if not isinstance(data, list) or not data:
            return "错误: 应为非空 JSON 数组"
        err = self._validate(data)
        if err:
            return f"错误: {err}"
        self.tasks.clear()
        self.task_order.clear()
        for i, td in enumerate(data):
            tid = str(td.get("id", i + 1)).strip()
            self.tasks[tid] = Task(id=tid, description=str(td["description"]).strip(),
                                   dependencies=[str(d).strip() for d in td.get("dependencies") or []])
            self.task_order.append(tid)
        return self._format()

    async def get_todo_list(self) -> str:
        """查看当前任务清单状态。"""
        return self._format()

    @staticmethod
    def _validate(data: list) -> str:
        seen: set[str] = set()
        deps_by_id: dict[str, list[str]] = {}
        for i, td in enumerate(data):
            if not isinstance(td, dict) or not str(td.get("description", "")).strip():
                return f"第 {i + 1} 项缺少 description"
            tid = str(td.get("id", i + 1)).strip()
            if tid in seen:
                return f"重复的任务 id: {tid}"
            seen.add(tid)
            deps_by_id[tid] = [str(d).strip() for d in td.get("dependencies") or []]
        for tid, deps in deps_by_id.items():
            for d in deps:
                if d not in seen:
                    return f"任务 {tid} 有未知依赖: {d}"
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(tid: str) -> str:
            if tid in done:
                return ""
            if tid in visiting:
                return f"检测到循环依赖（涉及任务 {tid}）"
            visiting.add(tid)
            for d in deps_by_id[tid]:
                if err := visit(d):
                    return err
            visiting.discard(tid)
            done.add(tid)
            return ""

        for tid in deps_by_id:
            if err := visit(tid):
                return err
        return ""

    def _format(self) -> str:
        if not self.tasks:
            return "任务清单为空"
        icons = {TaskStatus.PENDING: "⬜", TaskStatus.IN_PROGRESS: "🔄",
                 TaskStatus.COMPLETED: "✅", TaskStatus.FAILED: "❌"}
        lines = []
        for tid in self.task_order:
            t = self.tasks[tid]
            line = f"{icons[t.status]} [{t.id}] {t.description}"
            if t.dependencies:
                line += f"（依赖: {', '.join(t.dependencies)}）"
            if t.retry_count:
                line += f"[重试 {t.retry_count}/{t.max_retries}]"
            lines.append(line)
        completed = sum(1 for t in self.tasks.values() if t.status is TaskStatus.COMPLETED)
        lines.append(f"进度: {completed}/{len(self.tasks)}")
        return "\n".join(lines)

    def get_all_ready_tasks(self) -> list[Task]:
        ready = []
        for tid in self.task_order:
            t = self.tasks[tid]
            if t.status is TaskStatus.PENDING and all(
                    self.tasks[d].status is TaskStatus.COMPLETED
                    for d in t.dependencies if d in self.tasks):
                ready.append(t)
        return ready

    def mark_task_in_progress(self, tid: str) -> None:
        self.tasks[tid].status = TaskStatus.IN_PROGRESS

    def mark_task_complete(self, tid: str, result: str = "") -> None:
        self.tasks[tid].status = TaskStatus.COMPLETED
        self.tasks[tid].result = result

    def mark_task_failed(self, tid: str, reason: str) -> None:
        t = self.tasks[tid]
        t.failure_history.append(reason)
        t.retry_count += 1
        t.status = TaskStatus.FAILED if t.retry_count > t.max_retries else TaskStatus.PENDING

    def is_all_completed(self) -> bool:
        return bool(self.tasks) and all(t.status is TaskStatus.COMPLETED for t in self.tasks.values())

    def has_failed_tasks(self) -> bool:
        return any(t.status is TaskStatus.FAILED for t in self.tasks.values())

    def get_final_summary(self) -> str:
        if not self.tasks:
            return "Manager 没有创建可执行的任务。"
        completed = [t for t in self.tasks.values() if t.status is TaskStatus.COMPLETED]
        failed = [t for t in self.tasks.values() if t.status is TaskStatus.FAILED]
        lines = [f"已完成任务: {len(completed)}/{len(self.tasks)}"]
        for t in completed:
            lines.append(f"  [{t.id}] {t.description}")
            if t.result:
                lines += [f"      → {r}" for r in t.result.splitlines()]
        if failed:
            lines.append(f"失败任务: {len(failed)}")
            for t in failed:
                lines.append(f"  [{t.id}] {t.description}（重试 {t.retry_count} 次）")
                if t.failure_history:
                    lines.append(f"      最后失败原因: {t.failure_history[-1]}")
        return "\n".join(lines)


class _MessageBoard:
    """并行 Worker 共享消息板（单事件循环内，无需锁）。"""

    def __init__(self):
        self._messages: list[dict] = []

    async def post(self, worker_id: str, task_desc: str, message: str, status: str = "completed"):
        self._messages = [m for m in self._messages
                          if not (m["worker_id"] == worker_id and m["status"] != "completed")]
        self._messages.append({"worker_id": worker_id, "task": task_desc,
                               "message": message, "status": status})

    async def get_updates(self, exclude_worker: str | None = None) -> str:
        msgs = [m for m in self._messages if m["worker_id"] != exclude_worker]
        if not msgs:
            return ""
        return "\n".join(
            f"{'✅' if m['status'] == 'completed' else '🔄'} [{m['worker_id']}] {m['task']}\n   结果: {m['message']}"
            for m in msgs)


class _BoardTools:
    def __init__(self, board: _MessageBoard, worker_id: str, task_desc: str):
        self._board = board
        self._worker_id = worker_id
        self._task_desc = task_desc

    async def check_other_workers_progress(self) -> str:
        """查看其他并行 Worker 的进展与结果，避免重复劳动或基于其产出继续。"""
        return await self._board.get_updates(exclude_worker=self._worker_id) or "其他 Worker 暂无进展。"

    async def report_progress(self, message: str) -> str:
        """向共享消息板通报你的当前进展，供其他并行 Worker 参考。
        Parameters:
            message: 进展摘要
        """
        await self._board.post(self._worker_id, self._task_desc, message, status="in_progress")
        return "已通报进展。"


def _is_success(output: str) -> bool:
    return output.lstrip().startswith("SUCCESS:")


class WorkerOrchestrator:
    """Worker 执行编排：单任务（adhoc）与依赖分波并行。每个 Worker 复制独立 gf 状态文件。"""

    def __init__(self, *, task_manager: TaskManager, worker_model, workdir: Path,
                 make_tools, skills_manager):
        self._tm = task_manager
        self._worker_model = worker_model
        self._workdir = Path(workdir)
        self._make_tools = make_tools  # (state_file: Path) -> list[tool]
        self._skills_manager = skills_manager
        self._adhoc_seq = 0

    def _spawn_state(self, label) -> Path:
        main = self._workdir / "cli.json"
        state = self._workdir / f"worker_{label}_cli.json"
        if main.exists():
            shutil.copyfile(main, state)
        return state

    async def execute_task_with_worker(self, task_description: str, user_goal: str = "",
                                       retry_info: str = "") -> tuple[bool, str]:
        self._adhoc_seq += 1
        state = self._spawn_state(f"adhoc_{self._adhoc_seq}")
        agent = create_agent(self._worker_model, self._make_tools(state),
                             get_worker_system_prompt(self._skills_manager))
        prompt = f"[用户最终目标]\n{user_goal}\n\n[当前任务]\n{task_description}"
        if retry_info:
            prompt += f"\n\n这是重试。上次失败详情：\n{retry_info}\n请换一种方式完成。"
        token = ROLE.set(f"worker_adhoc_{self._adhoc_seq}")
        try:
            result = await agent.run(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return False, f"执行异常: {e}"
        finally:
            ROLE.reset(token)
        output = str(result.output or "")
        return _is_success(output), output

    async def execute_all_tasks_parallel(self, user_goal: str) -> str:
        board = _MessageBoard()
        for _wave in range(MAX_WAVES):
            ready = self._tm.get_all_ready_tasks()
            if not ready:
                break
            for t in ready:
                self._tm.mark_task_in_progress(t.id)
            sem = asyncio.Semaphore(MAX_WORKER_CONCURRENT)

            async def _run_one(task: Task):
                async with sem:
                    return await self._run_board_worker(task, board, user_goal)

            results = await asyncio.gather(*[_run_one(t) for t in ready], return_exceptions=True)
            for t, r in zip(ready, results):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, BaseException):
                    self._tm.mark_task_failed(t.id, f"执行异常: {r}")
                else:
                    success, output = r
                    if success:
                        self._tm.mark_task_complete(t.id, output)
                    else:
                        self._tm.mark_task_failed(t.id, output)
        return self._tm.get_final_summary()

    async def _run_board_worker(self, task: Task, board: _MessageBoard,
                                user_goal: str) -> tuple[bool, str]:
        worker_id = f"worker_{task.id}"
        state = self._spawn_state(task.id)
        tools = self._make_tools(state)
        bt = _BoardTools(board, worker_id, task.description)
        tools = tools + [bt.check_other_workers_progress, bt.report_progress]
        agent = create_agent(self._worker_model, tools,
                             get_worker_system_prompt(self._skills_manager, parallel=True))

        parts = [f"[用户最终目标]\n{user_goal}\n"]
        dep_parts = [f"[任务 {d}: {self._tm.tasks[d].description}]\n{self._tm.tasks[d].result}"
                     for d in task.dependencies
                     if d in self._tm.tasks and self._tm.tasks[d].status is TaskStatus.COMPLETED
                     and self._tm.tasks[d].result]
        if dep_parts:
            parts.append("[前置任务结果]\n" + "\n---\n".join(dep_parts) + "\n")
        updates = await board.get_updates(exclude_worker=worker_id)
        if updates:
            parts.append(f"[其他 Worker 进展]\n{updates}\n")
        parts.append(f"[当前任务]\n{task.description}")
        if task.retry_count:
            fails = "\n".join(f"  第{i + 1}次: {r}" for i, r in enumerate(task.failure_history))
            parts.append(f"\n这是第 {task.retry_count} 次重试。此前失败：\n{fails}\n请换一种方式。")
        prompt = "\n".join(parts)

        token = ROLE.set(worker_id)
        try:
            result = await agent.run(prompt, message_history=task.history)
        finally:
            ROLE.reset(token)
        task.history = result.all_messages()
        output = str(result.output or "")
        success = _is_success(output)
        await board.post(worker_id, task.description, output,
                         "completed" if success else "failed")
        return success, output
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_orchestrator.py -q` → 6 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/orchestrator.py backend/tests/test_agent_orchestrator.py
git commit -m "feat: Agent 任务编排——TaskManager 校验、消息板、Worker 并行波次与独立 gf 状态" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: goal.py——目标模式标记解析

**Files:**
- Create: `backend/app/agent/goal.py`
- Test: `backend/tests/test_agent_goal.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_goal.py`：
```python
from app.agent.goal import parse_goal


def test_continue():
    sig, text = parse_goal("第1轮推进\n<!-- REDLOTUS_GOAL:CONTINUE -->")
    assert sig == "CONTINUE" and text == "第1轮推进"


def test_done_case_insensitive_with_spaces():
    sig, text = parse_goal("完成 <!--  redlotus_goal : done  -->")
    assert sig == "DONE" and text == "完成"


def test_no_marker():
    sig, text = parse_goal("普通回复")
    assert sig is None and text == "普通回复"


def test_multiple_markers_last_wins_all_stripped():
    sig, text = parse_goal(
        "a <!-- REDLOTUS_GOAL:CONTINUE --> b <!-- REDLOTUS_GOAL:DONE -->")
    assert sig == "DONE"
    assert "REDLOTUS_GOAL" not in text and "a" in text and "b" in text
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_goal.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/goal.py`**

```python
"""目标模式标记协议：Agent 回合末尾的 CONTINUE/DONE 标记解析与剥离。"""
import re

GOAL_MARKER_RE = re.compile(r"<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->", re.IGNORECASE)


def parse_goal(text: str) -> tuple[str | None, str]:
    """返回 (信号, 剥离标记后的文本)。信号为 "CONTINUE"/"DONE"，无标记为 None。"""
    matches = GOAL_MARKER_RE.findall(text)
    cleaned = GOAL_MARKER_RE.sub("", text).strip()
    return (matches[-1].upper() if matches else None), cleaned
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_goal.py -q` → 4 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/goal.py backend/tests/test_agent_goal.py
git commit -m "feat: 目标模式标记解析（CONTINUE/DONE，剥离展示文本）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: system.py——AgentSystem 三角色装配

**Files:**
- Create: `backend/app/agent/system.py`
- Test: `backend/tests/test_agent_system.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_system.py`：
```python
import json

from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart,
                                  ToolReturnPart)
from pydantic_ai.models.function import FunctionModel

from app.agent.system import AgentSystem


def _tool_returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)]


def _system(tmp_path, coordinator, manager=None, worker=None):
    echo = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("SUCCESS: ok")]))
    return AgentSystem(
        models={"coordinator": coordinator, "manager": manager or echo, "worker": worker or echo},
        workdir=tmp_path, confirm_delete=False, emit=None)


async def test_direct_answer(tmp_path):
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("直答")]))
    system = _system(tmp_path, model)
    history, output = await system.run_turn("你好", [])
    assert output == "直答"
    assert len(history) >= 2  # 请求 + 响应


async def test_coordinator_uses_write_file_tool(tmp_path):
    def fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="write_file", args={"path": "out.txt", "content": "数据"})])
        return ModelResponse(parts=[TextPart("写好了")])

    system = _system(tmp_path, FunctionModel(fn))
    _, output = await system.run_turn("写个文件", [])
    assert output == "写好了"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "数据"


async def test_history_carries_across_turns(tmp_path):
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"轮{len(m)}")]))
    system = _system(tmp_path, model)
    h1, _ = await system.run_turn("一", [])
    h2, _ = await system.run_turn("二", h1)
    assert len(h2) > len(h1)


async def test_manager_three_phases(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    def coordinator_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="execute_task_with_manager", args={"user_input": "复杂任务"})])
        return ModelResponse(parts=[TextPart("汇报：" + str(_tool_returns(messages)[-1].content))])

    def manager_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="create_todo_list",
                args={"tasks_json": json.dumps([{"id": "1", "description": "子任务一"}])})])
        return ModelResponse(parts=[TextPart("最终报告：子任务一已完成")])

    system = _system(tmp_path, FunctionModel(coordinator_fn),
                     manager=FunctionModel(manager_fn),
                     worker=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("SUCCESS: 子任务一完成")])))
    _, output = await system.run_turn("做个复杂任务", [])
    assert "最终报告" in output
    assert system.task_manager.is_all_completed()


async def test_adhoc_worker_routing(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    def coordinator_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="execute_task_with_worker", args={"task_description": "单任务"})])
        return ModelResponse(parts=[TextPart("done")])

    system = _system(tmp_path, FunctionModel(coordinator_fn))
    _, output = await system.run_turn("派个活", [])
    assert output == "done"
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_system.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/system.py`**

```python
"""AgentSystem：每个回合批次装配一次的三角色系统（coordinator 路由 + Manager 三阶段）。"""
from pathlib import Path

from pydantic_ai.messages import PartDeltaEvent, TextPartDelta

from app.agent.factory import create_agent
from app.agent.orchestrator import TaskManager, WorkerOrchestrator
from app.agent.prompts import (get_coordinator_system_prompt, get_manager_system_prompt,
                               load_prompt)
from app.agent.skills import SKILLS_DIR, SkillsManager, SkillsToolkit
from app.agent.tools import ROLE, AgentToolkit


class AgentSystem:
    """models: {"coordinator"|"manager"|"worker": ModelConfig 或 pydantic-ai Model（测试）}"""

    def __init__(self, *, models: dict, workdir: Path, confirm_delete: bool, emit):
        self.models = models
        self.workdir = Path(workdir)
        self.emit = emit
        self._confirm_delete = confirm_delete
        self.skills_manager = SkillsManager(SKILLS_DIR)
        self.task_manager = TaskManager()
        self._manager_history: list = []
        self._main_state = self.workdir / "cli.json"
        self.orchestrator = WorkerOrchestrator(
            task_manager=self.task_manager, worker_model=models["worker"],
            workdir=self.workdir, make_tools=self._make_tools,
            skills_manager=self.skills_manager)

    def _make_tools(self, state_file: Path) -> list:
        tk = AgentToolkit(self.workdir, state_file, self._confirm_delete)
        sk = SkillsToolkit(self.skills_manager, state_file)
        return tk.tools + sk.tools

    async def run_turn(self, text: str, history: list) -> tuple[list, str]:
        """跑一轮 coordinator，返回 (新的全量历史, 输出文本)。"""
        tools = [self.execute_task_with_manager, self.execute_task_with_worker]
        tools += self._make_tools(self._main_state)
        agent = create_agent(self.models["coordinator"], tools,
                             get_coordinator_system_prompt(self.skills_manager))
        result = await agent.run(text, message_history=history,
                                 event_stream_handler=self._on_stream if self.emit else None)
        return result.all_messages(), str(result.output or "")

    async def _on_stream(self, ctx, events):
        async for ev in events:
            if (isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta)
                    and ev.delta.content_delta):
                await self.emit("delta", ev.delta.content_delta)

    async def execute_task_with_manager(self, user_input: str,
                                        continue_from_previous: bool = False) -> str:
        """把需要规划分解、多子任务并行的复杂请求交给 Manager 执行。
        Manager 会拆解任务清单，系统派多个 Worker 并行执行，最后产出面向用户的最终报告。
        Parameters:
            user_input: 完整的需求描述（新任务）或在上一轮结果上的新要求/反馈（续做）
            continue_from_previous: 是否在上一次 Manager 执行的基础上继续，默认 False
        """
        manager_agent = create_agent(
            self.models["manager"],
            [self.task_manager.create_todo_list, self.task_manager.get_todo_list],
            get_manager_system_prompt(self.skills_manager))

        if continue_from_previous:
            planning = load_prompt("manager_planning_continue.md").format(
                user_input=user_input, current_todo=await self.task_manager.get_todo_list())
        else:
            planning = load_prompt("manager_planning_new.md").format(user_input=user_input)

        token = ROLE.set("manager")
        try:
            result = await manager_agent.run(planning, message_history=self._manager_history)
            self._manager_history = result.all_messages()
        finally:
            ROLE.reset(token)

        final_summary = await self.orchestrator.execute_all_tasks_parallel(user_input)

        summary = load_prompt("manager_summary.md").format(
            user_input=user_input, final_summary=final_summary)
        token = ROLE.set("manager")
        try:
            result = await manager_agent.run(summary, message_history=self._manager_history)
            self._manager_history = result.all_messages()
        finally:
            ROLE.reset(token)
        return str(result.output or "")

    async def execute_task_with_worker(self, task_description: str, user_goal: str = "",
                                       retry_info: str = "") -> str:
        """把单个自包含任务交给一个 Worker 独立执行（带独立 gf 状态与工具沙盒）。
        返回以 SUCCESS:/FAILED: 开头的执行结果。
        Parameters:
            task_description: 清晰具体、自包含的任务描述
            user_goal: 用户的最终目标/大背景，帮 Worker 做取舍
            retry_info: 上次失败的细节（重试时填）
        """
        _success, output = await self.orchestrator.execute_task_with_worker(
            task_description, user_goal, retry_info)
        return output
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_system.py -q` → 5 passed。再跑全量回归：`uv run pytest -q` → 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/system.py backend/tests/test_agent_system.py
git commit -m "feat: AgentSystem——coordinator 路由 + Manager 三阶段 + 流式 delta 钩子" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: gf CLI 支持 GF_STATE_FILE

**Files:**
- Modify: `backend/app/cli.py:11`
- Test: `backend/tests/test_cli_state_env.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_cli_state_env.py`：
```python
import importlib
from pathlib import Path


def test_state_file_env_override(monkeypatch, tmp_path):
    from app import cli
    p = tmp_path / "s.json"
    monkeypatch.setenv("GF_STATE_FILE", str(p))
    importlib.reload(cli)
    assert cli.STATE_FILE == p
    monkeypatch.delenv("GF_STATE_FILE")
    importlib.reload(cli)
    assert cli.STATE_FILE == Path.home() / ".graphflow" / "cli.json"
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_cli_state_env.py -q` → AssertionError（STATE_FILE 仍是 home 路径）。

- [ ] **Step 3: 改 `backend/app/cli.py` 第 11 行**

```python
# 旧
STATE_FILE = Path.home() / ".graphflow" / "cli.json"
# 新
STATE_FILE = Path(os.environ.get("GF_STATE_FILE") or Path.home() / ".graphflow" / "cli.json")
```
（`os` 已在文件顶部 import，无需新增。）

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_cli_state_env.py tests/test_cli*.py -q` → 全部通过（含既有 CLI 测试无回归）。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/cli.py backend/tests/test_cli_state_env.py
git commit -m "feat: gf 状态文件支持 GF_STATE_FILE 环境变量（Agent 会话隔离基础）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: 数据模型、SSE payload 扩展、目标轮次配置

**Files:**
- Modify: `backend/app/models.py`（追加两个表）、`backend/app/events.py`（publish 加 **extra）、`backend/app/config.py`（加 agent_goal_max_rounds）
- Test: `backend/tests/test_agent_models_events.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_models_events.py`：
```python
import json

from sqlalchemy import select

from app import events
from app.config import settings
from app.models import AgentMessage, AgentSession


def test_publish_extra_payload():
    events.subscribers.clear()
    q = events.subscribe(7)
    events.publish(7, "agent", 3, kind="delta", data="嗨")
    assert json.loads(q.get_nowait()) == {"entity": "agent", "id": 3, "kind": "delta", "data": "嗨"}
    events.publish(7, "workflow", 5)  # 既有调用形态不受影响
    assert json.loads(q.get_nowait()) == {"entity": "workflow", "id": 5}
    events.unsubscribe(7, q)


def test_goal_rounds_setting_default():
    assert settings.agent_goal_max_rounds == 20


async def test_agent_tables(session_factory):
    async with session_factory() as s:
        sess = AgentSession(user_id=1, models_json='{"coordinator": 1, "manager": 1, "worker": 1}')
        s.add(sess)
        await s.commit()
        s.add(AgentMessage(session_id=sess.id, role="user", content_json='{"text": "hi"}'))
        await s.commit()
        row = (await s.execute(select(AgentSession))).scalar_one()
        assert row.status == "idle" and row.history_json == "[]" and row.title == ""
        msg = (await s.execute(select(AgentMessage))).scalar_one()
        assert msg.role == "user"
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_models_events.py -q` → ImportError / TypeError。

- [ ] **Step 3: 实现**

`backend/app/events.py` 的 publish 改为：
```python
def publish(user_id: int, entity: str, entity_id: int, **extra) -> None:
    payload = json.dumps({"entity": entity, "id": entity_id, **extra}, ensure_ascii=False)
    for q in subscribers.get(user_id, ()):
        q.put_nowait(payload)
```

`backend/app/config.py` 的 Settings 加一个字段（env 名即 `GRAPHFLOW_AGENT_GOAL_MAX_ROUNDS`）：
```python
class Settings(BaseSettings):
    model_config = {"env_prefix": "GRAPHFLOW_"}

    data_dir: Path = Path("data")
    secret_key: str = "dev-secret-change-me"
    agent_goal_max_rounds: int = 20
```

`backend/app/models.py` 文件末尾追加：
```python
class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(default="")
    models_json: Mapped[str] = mapped_column(Text)  # {"coordinator": 1, "manager": 1, "worker": 2}
    history_json: Mapped[str] = mapped_column(Text, default="[]")  # pydantic-ai ModelMessage 全量历史
    status: Mapped[str] = mapped_column(default="idle")  # idle / running
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    role: Mapped[str]  # user / assistant / tool
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_models_events.py tests/test_events*.py -q` → 全部通过（既有 events 测试无回归）。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/models.py backend/app/events.py backend/app/config.py backend/tests/test_agent_models_events.py
git commit -m "feat: AgentSession/AgentMessage 数据模型 + events.publish 扩展 payload + 目标轮次配置" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: turns.py——AgentTurnManager（后台回合 + 目标循环）

**Files:**
- Create: `backend/app/agent/turns.py`
- Test: `backend/tests/test_agent_turns.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_turns.py`（fake AgentSystem，不碰 pydantic-ai；`client` fixture 只用来 init_db）：
```python
import asyncio
import json

import pytest
from sqlalchemy import select

from app import events
from app.agent import turns
from app.config import settings
from app.models import AgentMessage, AgentSession


class FakeSystem:
    def __init__(self, outputs, error_at=None, delay=0.0):
        self.outputs = list(outputs)
        self.calls: list[str] = []
        self.error_at = error_at
        self.delay = delay

    async def run_turn(self, text, history):
        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error_at is not None and len(self.calls) == self.error_at:
            raise RuntimeError("boom")
        return history, self.outputs.pop(0)


@pytest.fixture
async def sid(client, session_factory):
    async with session_factory() as s:
        sess = AgentSession(user_id=1, models_json="{}", status="running")
        s.add(sess)
        await s.commit()
        return sess.id


async def _run(monkeypatch, sid, fake, text="开始", tm=None):
    tm = tm or turns.AgentTurnManager()
    monkeypatch.setattr(turns, "AgentSystem", lambda **kw: fake)
    tm.submit(sid, 1, text)
    task = tm.tasks[sid]
    await asyncio.wait_for(task, 10)
    return tm


async def _messages(session_factory, sid):
    async with session_factory() as s:
        rows = (await s.execute(select(AgentMessage).where(
            AgentMessage.session_id == sid).order_by(AgentMessage.id))).scalars().all()
        return [(r.role, json.loads(r.content_json)) for r in rows]


async def test_normal_turn(monkeypatch, session_factory, sid):
    fake = FakeSystem(["你好！"])
    await _run(monkeypatch, sid, fake)
    msgs = await _messages(session_factory, sid)
    assert msgs == [("assistant", {"text": "你好！"})]
    async with session_factory() as s:
        sess = await s.get(AgentSession, sid)
        assert sess.status == "idle" and sess.history_json == "[]"


async def test_goal_auto_continue_until_done(monkeypatch, session_factory, sid):
    fake = FakeSystem(["a <!-- REDLOTUS_GOAL:CONTINUE -->",
                       "b <!-- REDLOTUS_GOAL:CONTINUE -->",
                       "c <!-- REDLOTUS_GOAL:DONE -->"])
    await _run(monkeypatch, sid, fake)
    assert fake.calls == ["开始", "继续推进目标", "继续推进目标"]
    msgs = await _messages(session_factory, sid)
    texts = [c["text"] for _, c in msgs]
    assert texts == ["a", "b", "c"]  # 标记已剥离


async def test_goal_round_cap_wrapup(monkeypatch, session_factory, sid):
    monkeypatch.setattr(settings, "agent_goal_max_rounds", 1)
    fake = FakeSystem(["a <!-- REDLOTUS_GOAL:CONTINUE -->",
                       "b <!-- REDLOTUS_GOAL:CONTINUE -->",
                       "上限收尾 <!-- REDLOTUS_GOAL:CONTINUE -->"])  # 收尾轮的标记也被忽略
    await _run(monkeypatch, sid, fake)
    assert len(fake.calls) == 3
    assert fake.calls[1] == "继续推进目标"
    assert fake.calls[2].startswith("已达自动续轮上限")
    msgs = await _messages(session_factory, sid)
    assert len(msgs) == 3 and msgs[-1][1]["text"] == "上限收尾"


async def test_goal_stop(monkeypatch, session_factory, sid):
    fake = FakeSystem(["a <!-- REDLOTUS_GOAL:CONTINUE -->", "不该到这"])
    tm = turns.AgentTurnManager()
    monkeypatch.setattr(turns, "AgentSystem", lambda **kw: fake)
    tm.submit(sid, 1, "开始")
    tm.request_stop(sid)  # 任务尚未开始跑（未让出事件循环），确定性生效
    await asyncio.wait_for(tm.tasks[sid], 10)
    assert fake.calls == ["开始"]
    msgs = await _messages(session_factory, sid)
    assert msgs[-1][1]["text"].startswith("目标模式已被用户停止")


async def test_error_recorded(monkeypatch, session_factory, sid):
    fake = FakeSystem(["x"], error_at=1)
    await _run(monkeypatch, sid, fake)
    msgs = await _messages(session_factory, sid)
    assert msgs[-1][1]["text"].startswith("执行出错: ")
    async with session_factory() as s:
        assert (await s.get(AgentSession, sid)).status == "idle"


async def test_emit_persists_tool_end(client, session_factory, sid):
    events.subscribers.clear()
    q = events.subscribe(1)
    tm = turns.AgentTurnManager()
    emit = tm._make_emit(sid, 1)
    await emit("tool_start", {"tool": "run_command", "args_brief": "gf st", "agent_role": "coordinator"})
    await emit("tool_end", {"tool": "run_command", "args_brief": "gf st",
                            "agent_role": "coordinator", "status": "ok", "output_brief": "ok"})
    msgs = await _messages(session_factory, sid)
    assert msgs[-1][0] == "tool" and msgs[-1][1]["status"] == "ok"
    kinds = [json.loads(q.get_nowait())["kind"] for _ in range(2)]
    assert kinds == ["tool_start", "tool_end"]
    events.unsubscribe(1, q)


async def test_resume_interrupted(client, session_factory):
    async with session_factory() as s:
        sess = AgentSession(user_id=1, models_json="{}", status="running")
        s.add(sess)
        await s.commit()
        sid2 = sess.id
    n = await turns.resume_interrupted(session_factory)
    assert n >= 1
    async with session_factory() as s:
        assert (await s.get(AgentSession, sid2)).status == "idle"
    msgs = await _messages(session_factory, sid2)
    assert msgs[-1][1]["text"] == "回合因服务重启中断"
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_turns.py -q` → ModuleNotFoundError。

- [ ] **Step 3: 实现 `backend/app/agent/turns.py`**

```python
"""AgentTurnManager：后台回合执行、目标模式自动续轮、停止与重启恢复。"""
import asyncio
import json
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import select

from app.agent.goal import parse_goal
from app.agent.system import AgentSystem
from app.agent.tools import EMIT
from app.config import settings
from app.db import get_session_factory
from app.events import publish
from app.models import AgentMessage, AgentSession, ModelConfig


def session_dir(session_id: int) -> Path:
    return settings.data_dir / "agent" / str(session_id)


class AgentTurnManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.stop_flags: set[int] = set()

    def submit(self, session_id: int, user_id: int, text: str) -> None:
        self.stop_flags.discard(session_id)
        task = asyncio.create_task(self._run_turn(session_id, user_id, text))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    def request_stop(self, session_id: int) -> None:
        self.stop_flags.add(session_id)

    def cancel(self, session_id: int) -> None:
        task = self.tasks.get(session_id)
        if task:
            task.cancel()

    def _make_emit(self, session_id: int, user_id: int):
        async def emit(kind: str, data=None):
            if kind == "tool_end":  # 工具消息落库先于 publish（spec §7）
                async with get_session_factory()() as s:
                    s.add(AgentMessage(session_id=session_id, role="tool",
                                       content_json=json.dumps(data, ensure_ascii=False)))
                    await s.commit()
            publish(user_id, "agent", session_id, kind=kind, data=data)
        return emit

    async def _add_message(self, session_id: int, user_id: int, role: str, content: dict) -> None:
        async with get_session_factory()() as s:
            s.add(AgentMessage(session_id=session_id, role=role,
                               content_json=json.dumps(content, ensure_ascii=False)))
            await s.commit()
        publish(user_id, "agent", session_id, kind="message")

    async def _run_turn(self, session_id: int, user_id: int, text: str) -> None:
        sf = get_session_factory()
        async with sf() as s:
            sess = await s.get(AgentSession, session_id)
            history = ModelMessagesTypeAdapter.validate_json(sess.history_json)
            models = {role: await s.get(ModelConfig, mid)
                      for role, mid in json.loads(sess.models_json).items()}
        emit = self._make_emit(session_id, user_id)
        EMIT.set(emit)
        system = AgentSystem(models=models, workdir=session_dir(session_id),
                             confirm_delete=text.startswith("确认"), emit=emit)
        rounds, capped, input_text = 0, False, text
        try:
            while True:
                history, output = await system.run_turn(input_text, history)
                signal, cleaned = parse_goal(output)
                await self._add_message(session_id, user_id, "assistant", {"text": cleaned})
                if capped or signal != "CONTINUE":
                    break
                if session_id in self.stop_flags:
                    await self._add_message(session_id, user_id, "assistant",
                                            {"text": f"目标模式已被用户停止（第 {rounds + 1} 轮）"})
                    break
                if rounds >= settings.agent_goal_max_rounds:
                    capped = True
                    input_text = (f"已达自动续轮上限（{settings.agent_goal_max_rounds} 轮），"
                                  "请总结当前进展并结束本回合，等待用户决定是否继续。")
                    continue
                rounds += 1
                publish(user_id, "agent", session_id, kind="goal_round", data=rounds)
                input_text = "继续推进目标"
        except Exception as e:
            await self._add_message(session_id, user_id, "assistant", {"text": f"执行出错: {e}"})
        finally:
            async with sf() as s:
                sess = await s.get(AgentSession, session_id)
                if sess is not None:  # 会话可能在回合中被删除（任务被 cancel）
                    sess.history_json = ModelMessagesTypeAdapter.dump_json(history).decode()
                    sess.status = "idle"
                    await s.commit()
            publish(user_id, "agent", session_id, kind="turn_done")


turn_manager = AgentTurnManager()


async def resume_interrupted(session_factory) -> int:
    """进程启动时把 running 会话重置为 idle 并补一条中断说明（回合内存态无法续跑）。"""
    async with session_factory() as s:
        rows = (await s.execute(select(AgentSession).where(
            AgentSession.status == "running"))).scalars().all()
        for sess in rows:
            sess.status = "idle"
            s.add(AgentMessage(session_id=sess.id, role="assistant",
                               content_json=json.dumps({"text": "回合因服务重启中断"},
                                                       ensure_ascii=False)))
        await s.commit()
    return len(rows)
```

- [ ] **Step 4: 跑测试确认通过**

`uv run pytest tests/test_agent_turns.py -q` → 8 passed。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/agent/turns.py backend/tests/test_agent_turns.py
git commit -m "feat: AgentTurnManager——后台回合、目标模式续轮/上限/停止、重启恢复" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: /api/agent 路由与应用接线

**Files:**
- Create: `backend/app/routers/agent.py`
- Modify: `backend/app/main.py`（include router + lifespan 恢复）
- Test: `backend/tests/test_agent_api.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_api.py`：
```python
import json

import pytest

from app.agent import turns
from app.auth import parse_session_cookie


@pytest.fixture
async def mc_id(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "m1", "model_name": "qwen", "base_url": "http://llm.local/v1", "api_key": "sk"})
    return r.json()["id"]


@pytest.fixture
def no_run(monkeypatch):
    calls = []
    monkeypatch.setattr(turns.turn_manager, "submit",
                        lambda sid, uid, text: calls.append((sid, uid, text)))
    return calls


async def test_create_and_get_session(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["models"] == {"coordinator": mc_id, "manager": mc_id, "worker": mc_id}
    # cli.json 已生成：server 取自请求 base_url，cookie 可验签回本人
    wd = turns.session_dir(sid)
    state = json.loads((wd / "cli.json").read_text(encoding="utf-8"))
    assert state["server"] == "http://test"
    assert parse_session_cookie(state["cookie"]) is not None
    r = await auth_client.get("/api/agent/sessions")
    assert [s["id"] for s in r.json()] == [sid]
    r = await auth_client.get(f"/api/agent/sessions/{sid}")
    assert r.json()["messages"] == []


async def test_create_session_per_role_models(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={
        "models": {"coordinator": mc_id, "manager": mc_id, "worker": mc_id}})
    assert r.status_code == 200


async def test_create_session_bad_model(auth_client, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": 999})
    assert r.status_code == 422


async def test_message_flow_and_409(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    text = "帮我搭一个翻译流水线，把 q 列翻译成英文并跑起来"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": text})
    assert r.status_code == 200
    assert no_run == [(sid, 1, text)]
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "running"
    assert detail["title"] == text[:30]
    assert detail["messages"][0]["role"] == "user"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "再来"})
    assert r.status_code == 409


async def test_stop_endpoint(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    r = await auth_client.post(f"/api/agent/sessions/{sid}/stop")
    assert r.status_code == 200
    assert sid in turns.turn_manager.stop_flags


async def test_delete_cleans_workdir(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "hi"})
    wd = turns.session_dir(sid)
    assert wd.exists()
    r = await auth_client.delete(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200
    assert not wd.exists()
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404


async def test_cross_user_isolation(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404
    assert (await auth_client.post(f"/api/agent/sessions/{sid}/messages",
                                   json={"text": "x"})).status_code == 404
    assert (await auth_client.get("/api/agent/sessions")).json() == []
```

- [ ] **Step 2: 跑测试确认失败**

`uv run pytest tests/test_agent_api.py -q` → 404（路由不存在）。

- [ ] **Step 3: 实现 `backend/app/routers/agent.py`**

```python
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.turns import session_dir, turn_manager
from app.auth import get_current_user, make_session_cookie
from app.db import get_session
from app.events import publish
from app.models import AgentMessage, AgentSession, ModelConfig, User

router = APIRouter(prefix="/api/agent", tags=["agent"])

ROLES = ("coordinator", "manager", "worker")


class SessionIn(BaseModel):
    model_config_id: int | None = None
    models: dict[str, int] | None = None


class MessageIn(BaseModel):
    text: str


def _out(sess: AgentSession) -> dict:
    return {"id": sess.id, "title": sess.title, "status": sess.status,
            "models": json.loads(sess.models_json),
            "created_at": sess.created_at.isoformat(),
            "updated_at": sess.updated_at.isoformat()}


async def _get_owned(sid: int, user: User, session: AsyncSession) -> AgentSession:
    sess = await session.get(AgentSession, sid)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


async def _check_models(models: dict, user: User, session: AsyncSession) -> None:
    for role in ROLES:
        mc = await session.get(ModelConfig, models.get(role) or 0)
        if mc is None or mc.user_id != user.id:
            raise HTTPException(status_code=422, detail=f"角色 {role} 的模型配置无效")


@router.post("/sessions")
async def create_session(body: SessionIn, request: Request,
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    models = body.models or {r: body.model_config_id for r in ROLES}
    await _check_models(models, user, session)
    sess = AgentSession(user_id=user.id, models_json=json.dumps(models))
    session.add(sess)
    await session.commit()
    wd = session_dir(sess.id)
    wd.mkdir(parents=True, exist_ok=True)
    server = str(request.base_url).rstrip("/")
    (wd / "cli.json").write_text(
        json.dumps({"server": server, "cookie": make_session_cookie(user.id)}),
        encoding="utf-8")
    return _out(sess)


@router.get("/sessions")
async def list_sessions(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(AgentSession).where(AgentSession.user_id == user.id)
        .order_by(AgentSession.id.desc()))).scalars().all()
    return [_out(s) for s in rows]


@router.get("/sessions/{sid}")
async def get_session_detail(sid: int, user: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    msgs = (await session.execute(
        select(AgentMessage).where(AgentMessage.session_id == sid)
        .order_by(AgentMessage.id))).scalars().all()
    return {**_out(sess), "messages": [
        {"id": m.id, "role": m.role, "content": json.loads(m.content_json),
         "created_at": m.created_at.isoformat()} for m in msgs]}


@router.post("/sessions/{sid}/messages")
async def post_message(sid: int, body: MessageIn,
                       user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    sess = await _get_owned(sid, user, session)
    if sess.status == "running":
        raise HTTPException(status_code=409, detail="回合进行中")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空")
    await _check_models(json.loads(sess.models_json), user, session)
    session.add(AgentMessage(session_id=sid, role="user",
                             content_json=json.dumps({"text": text}, ensure_ascii=False)))
    if not sess.title:
        sess.title = text[:30]
    sess.status = "running"
    await session.commit()
    publish(user.id, "agent", sid, kind="message")
    turn_manager.submit(sid, user.id, text)
    return {"ok": True}


@router.post("/sessions/{sid}/stop")
async def stop_session(sid: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    await _get_owned(sid, user, session)
    turn_manager.request_stop(sid)
    return {"ok": True}


@router.delete("/sessions/{sid}")
async def delete_session(sid: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    await _get_owned(sid, user, session)
    turn_manager.cancel(sid)
    await session.execute(sa_delete(AgentMessage).where(AgentMessage.session_id == sid))
    await session.execute(sa_delete(AgentSession).where(AgentSession.id == sid))
    await session.commit()
    shutil.rmtree(session_dir(sid), ignore_errors=True)
    return {"ok": True}
```

- [ ] **Step 4: 接线 `backend/app/main.py`**

```python
# import 区
from app.agent.turns import resume_interrupted
from app.routers import agent, auth, datasets, events, model_configs, runs, workflows

# lifespan 内 resume_unfinished 之后加一行
    await resume_interrupted(get_session_factory())

# create_app 内（events 之前任意位置）加
    app.include_router(agent.router)
```

- [ ] **Step 5: 跑测试确认通过**

`uv run pytest tests/test_agent_api.py -q` → 7 passed；`uv run pytest -q` 全量回归通过。

- [ ] **Step 6: 提交**

```powershell
git add backend/app/routers/agent.py backend/app/main.py backend/tests/test_agent_api.py
git commit -m "feat: /api/agent 会话路由——建会话写 cli.json、消息触发回合、stop/删除/恢复接线" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: 前端 AgentDrawer

**Files:**
- Create: `frontend/src/agent/parse.ts`、`frontend/src/agent/parse.test.ts`、`frontend/src/agent/AgentDrawer.tsx`
- Modify: `frontend/src/api/events.ts`、`frontend/src/api/types.ts`、`frontend/src/App.tsx`、`frontend/package.json`（react-markdown）

- [ ] **Step 1: 装依赖**

```powershell
cd frontend; npm install react-markdown
```

- [ ] **Step 2: 写失败测试 `frontend/src/agent/parse.test.ts`**

```ts
import { describe, expect, it } from 'vitest'
import { extractConfirmDeletes, stripGoalMarkers } from './parse'

describe('stripGoalMarkers', () => {
  it('去掉 CONTINUE/DONE 标记', () => {
    expect(stripGoalMarkers('推进中 <!-- REDLOTUS_GOAL:CONTINUE -->')).toBe('推进中')
    expect(stripGoalMarkers('完成 <!--  redlotus_goal : DONE -->')).toBe('完成')
  })
  it('无标记原样返回', () => {
    expect(stripGoalMarkers('普通回复')).toBe('普通回复')
  })
})

describe('extractConfirmDeletes', () => {
  it('提取确认命令并从正文移除', () => {
    const r = extractConfirmDeletes('要删两个资源\n[confirm_delete] gf data rm 种子集\n[confirm_delete] gf wf rm 旧流水线')
    expect(r.commands).toEqual(['gf data rm 种子集', 'gf wf rm 旧流水线'])
    expect(r.text).toBe('要删两个资源')
  })
  it('无确认块', () => {
    expect(extractConfirmDeletes('正常').commands).toEqual([])
  })
})
```

- [ ] **Step 3: 跑测试确认失败**

`npx vitest run src/agent` → 找不到 ./parse。

- [ ] **Step 4: 实现 `frontend/src/agent/parse.ts`**

```ts
const GOAL_MARKER = /<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->/gi
const CONFIRM = /^\[confirm_delete\]\s*(.+)$/gm

export function stripGoalMarkers(text: string): string {
  return text.replace(GOAL_MARKER, '').trim()
}

export function extractConfirmDeletes(text: string): { text: string; commands: string[] } {
  const commands: string[] = []
  const cleaned = text.replace(CONFIRM, (_, cmd: string) => {
    commands.push(cmd.trim())
    return ''
  }).trim()
  return { text: cleaned, commands }
}
```

`npx vitest run src/agent` → 4 passed。

- [ ] **Step 5: 扩展类型**

`frontend/src/api/events.ts` 全文替换为：
```ts
import { useEffect, useRef } from 'react'

export interface GfEvent {
  entity: 'workflow' | 'model' | 'dataset' | 'run' | 'agent'
  id: number
  kind?: string
  data?: unknown
}

export function useEvents(handler: (e: GfEvent) => void) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const es = new EventSource('/api/events')
    es.onmessage = (m) => ref.current(JSON.parse(m.data) as GfEvent)
    return () => es.close()
  }, [])
}
```

`frontend/src/api/types.ts` 末尾追加：
```ts
export interface AgentToolContent {
  tool: string; args_brief: string; agent_role: string
  status?: 'ok' | 'error' | 'running'; output_brief?: string
}
export interface AgentMessageOut {
  id: number; role: 'user' | 'assistant' | 'tool'
  content: { text?: string } & Partial<AgentToolContent>
  created_at: string
}
export interface AgentSessionSummary {
  id: number; title: string; status: string
  models: Record<string, number>; created_at: string; updated_at: string
}
export interface AgentSessionDetail extends AgentSessionSummary { messages: AgentMessageOut[] }
```

- [ ] **Step 6: 实现 `frontend/src/agent/AgentDrawer.tsx`**

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Collapse, Drawer, FloatButton, Input, Select, Space, Spin, Tag, message } from 'antd'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import { useEvents } from '../api/events'
import type {
  AgentMessageOut, AgentSessionDetail, AgentSessionSummary, AgentToolContent, ModelConfig,
} from '../api/types'
import { extractConfirmDeletes, stripGoalMarkers } from './parse'

const ROLES = ['coordinator', 'manager', 'worker'] as const

export default function AgentDrawer() {
  const [open, setOpen] = useState(false)
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([])
  const [detail, setDetail] = useState<AgentSessionDetail | null>(null)
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [advanced, setAdvanced] = useState(false)
  const [roleSel, setRoleSel] = useState<Record<string, number | undefined>>({})
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState('')
  const [liveTools, setLiveTools] = useState<AgentToolContent[]>([])
  const [goalRound, setGoalRound] = useState(0)
  const sessionIdRef = useRef<number | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const refreshDetail = useCallback(async (sid: number) => {
    setDetail(await api.get<AgentSessionDetail>(`/api/agent/sessions/${sid}`))
  }, [])

  const selectSession = useCallback(async (sid: number) => {
    sessionIdRef.current = sid
    setStreaming('')
    setLiveTools([])
    setGoalRound(0)
    await refreshDetail(sid)
  }, [refreshDetail])

  useEffect(() => {
    if (!open) return
    void api.get<AgentSessionSummary[]>('/api/agent/sessions').then((list) => {
      setSessions(list)
      if (list.length && sessionIdRef.current === null) void selectSession(list[0].id)
    })
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [open, selectSession])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [detail?.messages.length, streaming, liveTools.length])

  useEvents((e) => {
    if (e.entity !== 'agent' || e.id !== sessionIdRef.current) return
    if (e.kind === 'delta') setStreaming((s) => s + String(e.data ?? ''))
    else if (e.kind === 'tool_start') {
      const d = e.data as AgentToolContent
      setLiveTools((ts) => [...ts, { ...d, status: 'running' }])
    } else if (e.kind === 'tool_end') {
      const d = e.data as AgentToolContent
      setLiveTools((ts) => {
        const i = ts.findIndex((t) => t.status === 'running' && t.tool === d.tool && t.agent_role === d.agent_role)
        if (i < 0) return [...ts, d]
        const next = ts.slice()
        next[i] = d
        return next
      })
    } else if (e.kind === 'message') {
      setStreaming('')
      setLiveTools([])
      void refreshDetail(e.id)
    } else if (e.kind === 'goal_round') setGoalRound(Number(e.data) || 0)
    else if (e.kind === 'turn_done') {
      setStreaming('')
      setLiveTools([])
      setGoalRound(0)
      void refreshDetail(e.id)
    }
  })

  const newSession = async () => {
    const useAdvanced = advanced && ROLES.every((r) => roleSel[r])
    if (!useAdvanced && !modelSel) {
      message.warning('先选择模型配置')
      return
    }
    const body = useAdvanced ? { models: roleSel } : { model_config_id: modelSel }
    try {
      const s = await api.post<AgentSessionSummary>('/api/agent/sessions', body)
      setSessions((list) => [s, ...list])
      await selectSession(s.id)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const send = async (text: string) => {
    const sid = sessionIdRef.current
    if (!sid || !text.trim()) return
    try {
      await api.post(`/api/agent/sessions/${sid}/messages`, { text })
      setInput('')
      await refreshDetail(sid)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const stop = async () => {
    if (sessionIdRef.current) await api.post(`/api/agent/sessions/${sessionIdRef.current}/stop`)
  }

  const running = detail?.status === 'running'

  const renderToolEntry = (t: AgentToolContent, key: string | number) => (
    <Collapse
      key={key}
      size="small"
      style={{ marginBottom: 4 }}
      items={[{
        key: '1',
        label: (
          <span style={{ fontSize: 12 }}>
            ⚙ {t.args_brief || t.tool} {t.status === 'ok' ? '✓' : t.status === 'error' ? '✗' : '…'}
            <Tag style={{ marginLeft: 8 }}>{t.agent_role}</Tag>
          </span>
        ),
        children: <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, margin: 0 }}>{t.output_brief || '(运行中)'}</pre>,
      }]}
    />
  )

  const renderMessage = (m: AgentMessageOut) => {
    if (m.role === 'tool') return renderToolEntry(m.content as AgentToolContent, m.id)
    const raw = m.content.text ?? ''
    if (m.role === 'user') {
      return (
        <div key={m.id} style={{ textAlign: 'right', margin: '8px 0' }}>
          <span style={{ background: '#e6f4ff', borderRadius: 8, padding: '6px 10px', display: 'inline-block', whiteSpace: 'pre-wrap' }}>{raw}</span>
        </div>
      )
    }
    const { text, commands } = extractConfirmDeletes(stripGoalMarkers(raw))
    return (
      <div key={m.id} style={{ margin: '8px 0' }}>
        <ReactMarkdown>{text}</ReactMarkdown>
        {commands.map((cmd) => (
          <Button key={cmd} danger size="small" style={{ marginRight: 8 }} disabled={running}
                  onClick={() => void send(`确认：${cmd}`)}>
            确认删除：{cmd}
          </Button>
        ))}
      </div>
    )
  }

  return (
    <>
      <FloatButton type="primary" style={{ right: 24, bottom: 24 }}
                   icon={<span>❦</span>} onClick={() => setOpen(true)} />
      <Drawer open={open} onClose={() => setOpen(false)} width={440} mask={false}
              title={
                <Space>
                  <Select size="small" style={{ width: 160 }} placeholder="选择会话"
                          value={detail?.id} onChange={(v) => void selectSession(v)}
                          options={sessions.map((s) => ({ value: s.id, label: s.title || `会话 ${s.id}` }))} />
                  <Button size="small" onClick={() => void newSession()}>新建</Button>
                  <Select size="small" style={{ width: 120 }} placeholder="模型"
                          value={modelSel} onChange={setModelSel}
                          options={models.map((m) => ({ value: m.id, label: m.name }))} />
                  <Button size="small" type="text" onClick={() => setAdvanced(!advanced)}>高级</Button>
                </Space>
              }>
        {advanced && (
          <Space style={{ marginBottom: 8 }} wrap>
            {ROLES.map((r) => (
              <Select key={r} size="small" style={{ width: 130 }} placeholder={r}
                      value={roleSel[r]} onChange={(v) => setRoleSel({ ...roleSel, [r]: v })}
                      options={models.map((m) => ({ value: m.id, label: `${r}: ${m.name}` }))} />
            ))}
          </Space>
        )}
        <div style={{ height: 'calc(100% - 120px)', overflowY: 'auto' }}>
          {detail?.messages.map(renderMessage)}
          {liveTools.map((t, i) => renderToolEntry(t, `live-${i}`))}
          {streaming && <ReactMarkdown>{stripGoalMarkers(streaming)}</ReactMarkdown>}
          {running && !streaming && <Spin size="small" style={{ display: 'block', margin: 8 }} />}
          <div ref={bottomRef} />
        </div>
        <div style={{ position: 'absolute', bottom: 12, left: 16, right: 16 }}>
          {running && (
            <Space style={{ marginBottom: 6 }}>
              <Tag color="processing">红莲正在工作…{goalRound > 0 && `目标进行中 · 第 ${goalRound} 轮`}</Tag>
              {goalRound > 0 && <Button size="small" danger onClick={() => void stop()}>停止</Button>}
            </Space>
          )}
          <Space.Compact style={{ width: '100%' }}>
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} value={input} disabled={running || !detail}
                            onChange={(e) => setInput(e.target.value)}
                            onPressEnter={(e) => {
                              if (!e.shiftKey) {
                                e.preventDefault()
                                void send(input)
                              }
                            }}
                            placeholder={detail ? '让红莲帮你搭链路、配模型、跑数据…' : '先新建会话'} />
            <Button type="primary" disabled={running || !detail} onClick={() => void send(input)}>发送</Button>
          </Space.Compact>
        </div>
      </Drawer>
    </>
  )
}
```

- [ ] **Step 7: 挂载到 `frontend/src/App.tsx`**

```tsx
// import 区追加
import AgentDrawer from './agent/AgentDrawer'

// Shell 的 <Layout> 内、</Layout.Content> 之后追加一行
      <AgentDrawer />
```

- [ ] **Step 8: 验证**

```powershell
cd frontend; npx vitest run; npm run build
```
预期：测试全过、tsc + vite build 无错误。

- [ ] **Step 9: 提交**

```powershell
git add frontend/src/agent/ frontend/src/api/events.ts frontend/src/api/types.ts frontend/src/App.tsx frontend/package.json frontend/package-lock.json
git commit -m "feat: 前端 AgentDrawer——会话/流式消息/工具条目/确认删除按钮/目标轮次徽标" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: 端到端测试（真实 uvicorn + 脚本化 FunctionModel）

**Files:**
- Test: `backend/tests/test_agent_e2e.py`

- [ ] **Step 1: 写测试（先写后跑，e2e 直接验证全链路）**

`backend/tests/test_agent_e2e.py`：
```python
"""端到端：真实 uvicorn + 脚本化 FunctionModel coordinator 经 gf 子进程搭图 + SSE 序列 + gf 状态隔离。"""
import asyncio
import json

import httpx
import pytest
import uvicorn
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from app.agent import factory
from app.agent.tools import AgentToolkit
from app.config import settings


def _tool_returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)]


COMMANDS = ["gf wf add 翻译流水线", "gf use 翻译流水线", "gf node add input", "gf node add llm"]


def _coordinator_fn(messages, info):
    done = len(_tool_returns(messages))
    if done < len(COMMANDS):
        return ModelResponse(parts=[ToolCallPart(tool_name="run_command",
                                                 args={"command": COMMANDS[done]})])
    return ModelResponse(parts=[TextPart("已创建翻译流水线：input + llm 两个节点。")])


@pytest.fixture
async def live(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app import db, events
    events.subscribers.clear()
    await db.init_db()
    from app.main import create_app
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=0,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task
    await db.engine.dispose()


async def _login_and_model(c: httpx.AsyncClient) -> int:
    await c.post("/api/auth/login", json={"username": "tester"})
    r = await c.post("/api/models", json={
        "name": "fm-coordinator", "model_name": "fake", "base_url": "http://fake.local/v1",
        "api_key": "sk"})
    return r.json()["id"]


async def test_one_sentence_builds_graph_with_sse(live, monkeypatch):
    monkeypatch.setattr(factory, "create_model", lambda mc: FunctionModel(_coordinator_fn))
    async with httpx.AsyncClient(base_url=live, timeout=30) as c:
        mc_id = await _login_and_model(c)
        sid = (await c.post("/api/agent/sessions",
                            json={"model_config_id": mc_id})).json()["id"]

        sse: list[dict] = []

        async def collect():
            async with c.stream("GET", "/api/events") as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        sse.append(json.loads(line[6:]))

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.2)  # 等订阅建立

        await c.post(f"/api/agent/sessions/{sid}/messages",
                     json={"text": "帮我搭一个把 q 列翻译成英文的流水线"})

        for _ in range(120):  # 最多 60s：4 次 gf 子进程
            await asyncio.sleep(0.5)
            detail = (await c.get(f"/api/agent/sessions/{sid}")).json()
            if detail["status"] == "idle":
                break
        assert detail["status"] == "idle", "回合未在限时内完成"

        # 1) 图真的建出来了
        wfs = (await c.get("/api/workflows")).json()
        target = [w for w in wfs if w["name"] == "翻译流水线"]
        assert target, f"工作流未创建: {wfs}"
        graph = (await c.get(f"/api/workflows/{target[0]['id']}")).json()["graph"]
        assert len(graph["nodes"]) == 2

        # 2) 消息记录完整：4 条工具 + 1 条 assistant
        tool_msgs = [m for m in detail["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 4
        assert all(m["content"]["status"] == "ok" for m in tool_msgs)
        assert detail["messages"][-1]["content"]["text"].startswith("已创建翻译流水线")

        # 3) SSE 事件序列
        collector.cancel()
        agent_kinds = [e.get("kind") for e in sse if e.get("entity") == "agent"]
        assert "tool_start" in agent_kinds and "tool_end" in agent_kinds
        assert agent_kinds[-1] == "turn_done" or "turn_done" in agent_kinds
        # gf 操作也触发了既有实体事件（画布实时联动的依据）
        assert any(e.get("entity") == "workflow" for e in sse)


async def test_gf_state_isolation_two_sessions(live, tmp_path):
    async with httpx.AsyncClient(base_url=live, timeout=30) as c:
        await c.post("/api/auth/login", json={"username": "tester2"})
        cookie = c.cookies.get("gf_session")
        for name in ("流水线A", "流水线B"):
            await c.post("/api/workflows", json={"name": name})

        outs = []
        for i, wf in enumerate(("流水线A", "流水线B"), 1):
            wd = tmp_path / f"sess{i}"
            wd.mkdir()
            (wd / "cli.json").write_text(
                json.dumps({"server": live, "cookie": cookie}), encoding="utf-8")
            tk = AgentToolkit(wd, wd / "cli.json", confirm_delete=False)
            assert "Return code: 0" in await tk.run_command(f"gf use {wf}", timeout=60)
            outs.append(await tk.run_command("gf show", timeout=60))

        assert "流水线A" in outs[0] and "流水线B" not in outs[0]
        assert "流水线B" in outs[1] and "流水线A" not in outs[1]
```

（已对照源码核实：`POST /api/workflows` 接 `{name}`、`GET /api/workflows/{id}` 返回含 `graph`；`gf wf add <name>` / `gf use <ref>` / `gf node add <type>` / `gf show` 的语法与 `backend/app/cli.py:520-536` 一致，`gf show` 会打印工作流名。）

- [ ] **Step 2: 跑测试**

`uv run pytest tests/test_agent_e2e.py -q -x` → 2 passed。若 gf 子进程失败，先看 tool_msgs 里的 output_brief 排查（PYTHONPATH/server 地址/cookie）。

- [ ] **Step 3: 提交**

```powershell
git add backend/tests/test_agent_e2e.py
git commit -m "test: 端到端——一句话经 gf 真实搭图、SSE 事件序列、双会话 gf 状态隔离" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: 目标模式端到端、删除 RedLotus/、README、收尾回归

**Files:**
- Test: `backend/tests/test_agent_goal_e2e.py`
- Create: `README.md`（仓库根，原本不存在）
- Delete: `RedLotus/` 目录（未被 git 跟踪，直接删文件系统）

- [ ] **Step 1: 写目标模式端到端测试**

`backend/tests/test_agent_goal_e2e.py`（ASGI 级全栈：真路由 + 真 TurnManager + FunctionModel）：
```python
import asyncio

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from app.agent import factory, turns


def _user_prompt_count(messages):
    return sum(1 for m in messages if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, UserPromptPart))


def _goal_model():
    def fn(messages, info):
        n = _user_prompt_count(messages)
        if n <= 2:
            return ModelResponse(parts=[TextPart(f"第{n}轮推进 <!-- REDLOTUS_GOAL:CONTINUE -->")])
        return ModelResponse(parts=[TextPart("目标达成 <!-- REDLOTUS_GOAL:DONE -->")])
    return FunctionModel(fn)


async def _setup(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "fm", "model_name": "fake", "base_url": "http://fake.local/v1", "api_key": "sk"})
    mc_id = r.json()["id"]
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    return sid


async def test_goal_loop_full_stack(auth_client, monkeypatch):
    monkeypatch.setattr(factory, "create_model", lambda mc: _goal_model())
    sid = await _setup(auth_client)
    await auth_client.post(f"/api/agent/sessions/{sid}/messages",
                           json={"text": "把通过率调到 90% 以上"})
    await asyncio.wait_for(turns.turn_manager.tasks[sid], 20)

    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "idle"
    texts = [m["content"]["text"] for m in detail["messages"] if m["role"] == "assistant"]
    assert texts == ["第1轮推进", "第2轮推进", "目标达成"]
    assert all("REDLOTUS_GOAL" not in t for t in texts)


async def test_goal_user_message_resets_round(auth_client, monkeypatch):
    monkeypatch.setattr(factory, "create_model", lambda mc: _goal_model())
    sid = await _setup(auth_client)
    await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "目标一"})
    await asyncio.wait_for(turns.turn_manager.tasks[sid], 20)
    # 第二条用户消息开启新批次：轮次计数从 0 重新开始（不会立刻触顶）
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "确认：继续干"})
    assert r.status_code == 200
    await asyncio.wait_for(turns.turn_manager.tasks[sid], 20)
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "idle"
```

`uv run pytest tests/test_agent_goal_e2e.py -q` → 2 passed。

- [ ] **Step 2: 删除 RedLotus/ 并写 README**

```powershell
Remove-Item -Recurse -Force E:\代码\GraphFlow\RedLotus
```

新建仓库根 `README.md`：
````markdown
# GraphFlow

Dify 风格的节点式 LLM 训练数据合成平台：画布编排（输入→LLM 合成→自动处理→输出）、
数据集管理、模型配置（OpenAI 兼容）、运行与导出，外加 `gf` 命令行与内置 Agent「红莲」。

## 启动

```powershell
# 后端（端口 8000）
cd backend; uv sync; uv run uvicorn app.main:app --reload

# 前端（端口 5173，代理 /api 到 8000）
cd frontend; npm install; npm run dev
```

## gf CLI

```powershell
cd backend
uv run gf login <用户名>
uv run gf wf add 我的流水线; uv run gf node add input; uv run gf node add llm
uv run gf run
```
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

## 测试

```powershell
cd backend; uv run pytest -q
cd frontend; npx vitest run
```
````

- [ ] **Step 3: 验收对照（spec §15）+ 全量回归**

```powershell
cd backend; uv run pytest -q
cd ../frontend; npx vitest run; npm run build
```
逐条核对 spec §15 的 7 条验收标准，确认各有对应通过的测试/实现（见下方「验收对照」表）；确认 `uv pip list` 里没有 playwright/lancedb/textual。

- [ ] **Step 4: 提交并准备合并**

```powershell
git add backend/tests/test_agent_goal_e2e.py README.md
git commit -m "test: 目标模式全栈端到端 + README（RedLotus 源目录已删除）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
合并（由主会话在用户确认后执行）：
```powershell
git checkout master; git merge --no-ff feature/agent -m "merge: 原生 Agent 跑数平台（RedLotus 移植 + 目标模式）" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 验收对照（spec §15 → 实现/测试）

| # | 验收标准 | 对应 |
|---|---|---|
| 1 | 一句话搭流水线，画布实时变 | Task 15 `test_one_sentence_builds_graph_with_sse`（图建出 + workflow SSE 事件） |
| 2 | 关浏览器后台续跑、重开看记录 | Task 12 后台 task + 落库；Task 13 GET 历史；turn 不依赖连接 |
| 3 | 删除前确认按钮 + 未确认硬拦 | Task 4 `test_gf_delete_intercepted`、Task 6 提示词、Task 14 确认按钮 |
| 4 | 多会话 gf 不串号 | Task 15 `test_gf_state_isolation_two_sessions`、Task 13 cross-user 测试 |
| 5 | 三角色按会话 ModelConfig，key 不外泄 | Task 5（现解密现用，不落日志）、Task 13 `_check_models` 422 |
| 6 | RedLotus/ 删除、无重依赖 | Task 16 Step 2/3 |
| 7 | 目标模式自动续轮/上限/停止 | Task 12 单测 + Task 16 全栈端到端 |
