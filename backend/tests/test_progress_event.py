import json
from app import events
from app.engine.runner import _set_node_state


async def test_set_node_state_publishes_progress(session_factory):
    q = events.subscribe(1)
    try:
        await _set_node_state(session_factory, 1, "n1", user_id=1,
                              status="running", total=10, done=3, failed=0)
        payload = json.loads(q.get_nowait())
    finally:
        events.unsubscribe(1, q)
    assert payload["entity"] == "run" and payload["id"] == 1 and payload["kind"] == "progress"
    assert payload["data"] == {"node_id": "n1", "status": "running",
                               "total": 10, "done": 3, "failed": 0}
