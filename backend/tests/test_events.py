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


async def test_mutations_push_events(auth_client):
    from app import events

    me = (await auth_client.get("/api/me")).json()
    q = events.subscribe(me["id"])

    def popped():
        return json.loads(q.get_nowait())

    wf = (await auth_client.post("/api/workflows", json={"name": "流"})).json()
    assert popped() == {"entity": "workflow", "id": wf["id"]}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"name": "新名"})
    assert popped() == {"entity": "workflow", "id": wf["id"]}
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "", "default_params": {}})).json()
    assert popped() == {"entity": "model", "id": mc["id"]}
    files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    assert popped() == {"entity": "dataset", "id": ds["id"]}
    await auth_client.delete(f"/api/workflows/{wf['id']}")
    assert popped() == {"entity": "workflow", "id": wf["id"]}
