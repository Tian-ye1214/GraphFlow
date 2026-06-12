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
    # Windows 对 PATH 上的 gf.exe 不区分大小写，拦截也必须不区分
    assert "需用户确认" in await tk.run_command("GF data rm 种子集")


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
