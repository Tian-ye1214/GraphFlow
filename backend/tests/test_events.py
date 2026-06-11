import asyncio
import json


async def test_events_requires_auth(client):
    r = await client.get("/api/events")
    assert r.status_code == 401


async def test_stream_receives_published_event(auth_client):
    from app import events

    me = (await auth_client.get("/api/me")).json()
    async with auth_client.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        events.publish(me["id"], "workflow", 1)
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                assert json.loads(line[6:]) == {"entity": "workflow", "id": 1}
                break


async def test_events_isolated_per_user(auth_client):
    from app import events

    a_id = (await auth_client.get("/api/me")).json()["id"]
    async with auth_client.stream("GET", "/api/events"):
        events.publish(a_id + 999, "workflow", 1)  # 发给别的用户
        q = next(iter(events.subscribers[a_id]))
        assert q.qsize() == 0


async def test_disconnect_unsubscribes(auth_client):
    from app import events

    async with auth_client.stream("GET", "/api/events"):
        assert events.subscribers
    for _ in range(100):
        if not events.subscribers:
            break
        await asyncio.sleep(0.01)
    assert not events.subscribers
