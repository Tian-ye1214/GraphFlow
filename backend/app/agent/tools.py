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

from app.agent.data_preview import WorkflowDataPreview
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
GF_DELETE_RE = re.compile(r"gf\s+(wf|data|model)\s+rm\b", re.IGNORECASE)
GF_LOGIN_RE = re.compile(r"gf\s+login\b", re.IGNORECASE)
BACKGROUND_RE = re.compile(r"^\s*(start|nohup|setsid)\b|&\s*$", re.IGNORECASE)


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

    def __init__(self, workdir: Path, state_file: Path, confirm_delete: bool,
                 session_factory=None, user_id: int | None = None):
        self._workdir = Path(workdir)
        self._state_file = Path(state_file)
        self._confirm_delete = confirm_delete
        self._session_factory = session_factory
        self._user_id = user_id

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
        if GF_LOGIN_RE.search(cmd):
            return "会话已绑定当前用户，禁止用 gf login 切换身份；直接执行业务命令即可。"
        if lower == "gf" or lower.startswith("gf "):
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

    async def preview_workflow_data(self, workflow_id: int, node_id: str | None = None,
                                    source: str = "auto", limit: int = 5) -> str:
        """预览当前用户工作流的数据列和少量样例行，默认只返回前 5 行。
        Parameters:
            workflow_id: 工作流 ID
            node_id: 可选节点 ID；读取最近运行时返回该节点的真实输入样例
            source: auto / dataset / latest_run；auto 优先最近运行产出，否则回退输入数据集
            limit: 最大样例行数，默认 5，系统上限 20
        """
        if self._session_factory is None or self._user_id is None:
            return '{"source":"none","run_id":null,"columns":[],"rows":[],"truncated":false,"error":"preview_unavailable"}'
        return await WorkflowDataPreview(
            self._session_factory, self._user_id
        ).preview_workflow_data(workflow_id, node_id=node_id, source=source, limit=limit)

    @property
    def tools(self) -> list:
        return [self.read_file, self.write_file, self.list_directory,
                self.run_command, self.search_web, self.extract_file_content,
                self.preview_workflow_data]
