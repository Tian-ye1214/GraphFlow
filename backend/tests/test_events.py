import json

from app.auth import make_session_cookie


async def test_events_requires_auth(client):
    r = await client.get("/api/events")
    assert r.status_code == 401


async def test_stream_receives_published_event(auth_client):
    from app import events
    from app.routers.events import event_stream

    me = (await auth_client.get("/api/me")).json()
    resp = await event_stream(gf_session=make_session_cookie(me["id"]))
    assert resp.media_type == "text/event-stream"
    events.publish(me["id"], "workflow", 1)
    chunk = await anext(resp.body_iterator)
    assert json.loads(chunk.removeprefix("data: ").strip()) == {"entity": "workflow", "id": 1}
    await resp.body_iterator.aclose()
    assert me["id"] not in events.subscribers  # 断开即注销


async def test_events_isolated_per_user(auth_client):
    from app import events
    from app.routers.events import event_stream

    a_id = (await auth_client.get("/api/me")).json()["id"]
    resp = await event_stream(gf_session=make_session_cookie(a_id))
    events.publish(a_id + 999, "workflow", 1)  # 发给别的用户
    q = next(iter(events.subscribers[a_id]))
    assert q.qsize() == 0
    await resp.body_iterator.aclose()
