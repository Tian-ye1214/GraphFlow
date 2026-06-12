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
