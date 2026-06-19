import pytest


async def test_create_makes_v1_and_extracts_vars(auth_client):
    r = await auth_client.post("/api/prompts", json={"name": "问候", "description": "d", "body": "你好 {{name}} 与 {{name}} 和 {{age}}"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "问候" and d["current"]["version"] == 1
    assert d["current"]["body"] == "你好 {{name}} 与 {{name}} 和 {{age}}"
    assert d["current"]["variables"] == ["age", "name"]   # 去重排序


async def test_list_and_get(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x {{q}}"})).json()["id"]
    lst = (await auth_client.get("/api/prompts")).json()
    assert lst[0]["name"] == "P" and lst[0]["latest_version"] == 1 and lst[0]["variables"] == ["q"]
    d = (await auth_client.get(f"/api/prompts/{cid}")).json()
    assert d["current"]["body"] == "x {{q}}"


async def test_update_body_creates_new_version(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    d = (await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2 {{a}}"})).json()
    assert d["current"]["version"] == 2 and d["current"]["body"] == "v2 {{a}}"
    assert [v["version"] for v in d["versions"]] == [1, 2]


async def test_update_name_only_no_new_version(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "same"})).json()["id"]
    d = (await auth_client.put(f"/api/prompts/{cid}", json={"name": "P2", "description": "", "body": "same"})).json()
    assert d["name"] == "P2" and d["current"]["version"] == 1


async def test_delete(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    assert (await auth_client.delete(f"/api/prompts/{cid}")).status_code == 200
    assert (await auth_client.get(f"/api/prompts/{cid}")).status_code == 404


async def test_tenant_isolation(client):
    await client.post("/api/auth/login", json={"username": "u1"})
    cid = (await client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    await client.post("/api/auth/login", json={"username": "u2"})
    assert (await client.get(f"/api/prompts/{cid}")).status_code == 404
    assert (await client.put(f"/api/prompts/{cid}", json={"name": "x", "description": "", "body": "y"})).status_code == 404
    assert (await client.delete(f"/api/prompts/{cid}")).status_code == 404


async def test_versions_lists_all(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2"})
    vs = (await auth_client.get(f"/api/prompts/{cid}/versions")).json()
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[0]["body"] == "v1" and vs[1]["body"] == "v2"


async def test_rollback_creates_new_version_with_old_body(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1 {{a}}"})).json()["id"]
    await auth_client.put(f"/api/prompts/{cid}", json={"name": "P", "description": "", "body": "v2"})
    d = (await auth_client.post(f"/api/prompts/{cid}/rollback", json={"version": 1})).json()
    assert d["current"]["version"] == 3 and d["current"]["body"] == "v1 {{a}}"
    assert d["current"]["variables"] == ["a"]


async def test_rollback_unknown_version_404(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "v1"})).json()["id"]
    assert (await auth_client.post(f"/api/prompts/{cid}/rollback", json={"version": 9})).status_code == 404


async def test_duplicate_creates_new_prompt(auth_client):
    cid = (await auth_client.post("/api/prompts", json={"name": "原", "body": "正文 {{x}}"})).json()["id"]
    d = (await auth_client.post(f"/api/prompts/{cid}/duplicate", json={})).json()
    assert d["id"] != cid and d["name"] == "原 副本"
    assert d["current"]["version"] == 1 and d["current"]["body"] == "正文 {{x}}"
    named = (await auth_client.post(f"/api/prompts/{cid}/duplicate", json={"name": "自定义"})).json()
    assert named["name"] == "自定义"


async def test_used_by_lists_referencing_nodes(auth_client):
    pid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    wf = (await auth_client.post("/api/workflows", json={"name": "流"})).json()
    graph = {"nodes": [{"id": "n1", "type": "llm_synth", "position": {"x": 0, "y": 0},
                        "config": {"system_prompt_ref": pid}}], "edges": []}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    d = (await auth_client.get(f"/api/prompts/{pid}")).json()
    assert d["used_by"] == [{"workflow_id": wf["id"], "workflow_name": "流", "node_id": "n1", "slot": "system_prompt"}]


async def test_used_by_empty_when_unreferenced(auth_client):
    pid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    assert (await auth_client.get(f"/api/prompts/{pid}")).json()["used_by"] == []


async def test_get_prompt_with_dirty_workflow_graph_no_500(auth_client):
    """用户工作流图含畸形节点(缺 id / config 非 dict / 节点非 dict)时，只读 prompt 端点 _used_by 应跳过畸形项不 500。"""
    pid = (await auth_client.post("/api/prompts", json={"name": "P", "body": "x"})).json()["id"]
    wf = (await auth_client.post("/api/workflows", json={"name": "流"})).json()
    graph = {"nodes": [
        {"type": "llm_synth", "config": {"system_prompt_ref": pid}},   # 缺 id → 原 node["id"] KeyError
        {"id": "n2", "type": "llm_synth", "config": "oops"},            # config 非 dict → cfg.get AttributeError
        "not_a_node",                                                    # 节点非 dict → node.get AttributeError
    ], "edges": []}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    r = await auth_client.get(f"/api/prompts/{pid}")
    assert r.status_code == 200
