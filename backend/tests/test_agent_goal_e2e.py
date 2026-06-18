import asyncio

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from app.agent import factory, turns


def _user_prompt_count(messages):
    return sum(1 for m in messages if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, UserPromptPart))


# turn_manager 总是设 emit → run_turn 走 event_stream_handler（流式请求），
# 所以 FunctionModel 必须用 stream_function（产出文本块）。
def _goal_model():
    async def fn(messages, info):
        n = _user_prompt_count(messages)
        if n <= 2:
            yield f"第{n}轮推进 <!-- REDLOTUS_GOAL:CONTINUE -->"
        else:
            yield "目标达成 <!-- REDLOTUS_GOAL:DONE -->"
    return FunctionModel(stream_function=fn)


async def _setup(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "fm", "model_name": "fake", "base_url": "http://fake.local/v1", "api_key": "sk"})
    mc_id = r.json()["id"]
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    return sid


async def test_goal_loop_full_stack(auth_client, monkeypatch):
    monkeypatch.setattr(factory, "create_model", lambda mc, params=None: _goal_model())
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
    monkeypatch.setattr(factory, "create_model", lambda mc, params=None: _goal_model())
    sid = await _setup(auth_client)
    await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "目标一"})
    await asyncio.wait_for(turns.turn_manager.tasks[sid], 20)
    # 第二条用户消息开启新批次：轮次计数从 0 重新开始（不会立刻触顶）
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "确认：继续干"})
    assert r.status_code == 200
    await asyncio.wait_for(turns.turn_manager.tasks[sid], 20)
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "idle"
