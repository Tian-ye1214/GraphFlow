import json

import pytest
from sqlalchemy import select

from app.engine.graph import parse_graph
from app.engine.runner import _resolve_prompt_refs
from app.models import User


def _graph(config: dict) -> object:
    return parse_graph(json.dumps({
        "nodes": [{"id": "n1", "type": "llm_synth", "position": {"x": 0, "y": 0}, "config": config}],
        "edges": [],
    }))


async def _uid(session_factory) -> int:
    async with session_factory() as s:
        return (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()


async def test_resolve_injects_latest_body(auth_client, session_factory):
    p = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1 {{q}}"})).json()
    await auth_client.put(f"/api/prompts/{p['id']}", json={"name": "P", "description": "", "body": "v2 {{q}}"})
    graph = _graph({"system_prompt_ref": p["id"]})
    await _resolve_prompt_refs(session_factory, graph, await _uid(session_factory))
    assert graph.nodes[0].config["system_prompt"] == "v2 {{q}}"


async def test_resolve_missing_raises(auth_client, session_factory):
    graph = _graph({"user_prompt_ref": 99999})
    with pytest.raises(ValueError):
        await _resolve_prompt_refs(session_factory, graph, await _uid(session_factory))
