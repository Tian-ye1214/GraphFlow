GRAPH = {"nodes": [{"id": "n1", "type": "input", "config": {}}], "edges": []}


async def test_crud(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "测试流"})).json()
    assert wf["name"] == "测试流"
    r = await auth_client.put(f"/api/workflows/{wf['id']}", json={"name": "改名", "graph": GRAPH})
    assert r.status_code == 200
    got = (await auth_client.get(f"/api/workflows/{wf['id']}")).json()
    assert got["name"] == "改名"
    assert got["graph"]["nodes"][0]["id"] == "n1"
    assert len((await auth_client.get("/api/workflows")).json()) == 1
    await auth_client.delete(f"/api/workflows/{wf['id']}")
    assert (await auth_client.get("/api/workflows")).json() == []


async def test_save_incomplete_graph_allowed(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "半成品"})).json()
    bad = {"nodes": [{"id": "x", "type": "llm_synth", "config": {}}], "edges": []}
    assert (await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": bad})).status_code == 200


async def test_user_isolation(auth_client):
    await auth_client.post("/api/workflows", json={"name": "我的"})
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/workflows")).json() == []
