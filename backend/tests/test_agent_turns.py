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


def test_session_dir_absolute_under_relative_data_dir(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(settings, "data_dir", Path("data"))  # 生产默认就是相对路径
    p = turns.session_dir(7)
    assert p.is_absolute()  # 相对路径会被 gf 子进程按其 cwd 二次拼接（已实际踩坑）
    assert p.parts[-2:] == ("agent", "7")
