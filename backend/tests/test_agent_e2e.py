"""端到端：真实 uvicorn + 脚本化 FunctionModel coordinator 经 gf 子进程搭图 + SSE 序列 + gf 状态隔离。"""
import asyncio
import json

import httpx
import pytest
import uvicorn
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from app.agent import factory
from app.agent.tools import AgentToolkit
from app.config import settings


def _tool_returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)]


COMMANDS = ["gf wf add 翻译流水线", "gf use 翻译流水线", "gf node add input", "gf node add llm"]


# turn_manager 总是设 emit → run_turn 走 event_stream_handler（流式请求），
# 所以 FunctionModel 必须用 stream_function（产出 DeltaToolCall 或文本块）。
async def _coordinator_stream(messages, info):
    done = len(_tool_returns(messages))
    if done < len(COMMANDS):
        yield {0: DeltaToolCall(name="run_command",
                                json_args=json.dumps({"command": COMMANDS[done]}))}
    else:
        yield "已创建翻译流水线：input + llm 两个节点。"


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
    monkeypatch.setattr(factory, "create_model",
                        lambda mc, params=None: FunctionModel(stream_function=_coordinator_stream))
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

        tks = []
        for i, wf in enumerate(("流水线A", "流水线B"), 1):
            wd = tmp_path / f"sess{i}"
            wd.mkdir()
            (wd / "cli.json").write_text(
                json.dumps({"server": live, "cookie": cookie}), encoding="utf-8")
            tk = AgentToolkit(wd, wd / "cli.json", confirm_delete=False)
            assert "Return code: 0" in await tk.run_command(f"gf use {wf}", timeout=60)
            tks.append(tk)

        # 两次 use 之后再各自 show：若两份状态路径坍缩为同一个，后一次 use 会覆盖前者，这里必串台
        outs = [await tk.run_command("gf show", timeout=60) for tk in tks]

        assert "流水线A" in outs[0] and "流水线B" not in outs[0]
        assert "流水线B" in outs[1] and "流水线A" not in outs[1]
