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
    wf = (await auth_client.post("/api/workflows", json={"name": "我的"})).json()
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/workflows")).json() == []
    assert (await auth_client.get(f"/api/workflows/{wf['id']}")).status_code == 404


async def test_duplicate_workflow(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "原流"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": GRAPH})
    dup = (await auth_client.post(f"/api/workflows/{wf['id']}/duplicate")).json()
    assert dup["id"] != wf["id"]
    assert dup["name"] == "原流 副本"
    assert dup["graph"]["nodes"][0]["id"] == "n1"           # 图被完整克隆
    # 同形返回 + 列表里两条独立工作流
    assert {"id", "name", "graph", "updated_at"} <= set(dup)
    assert len((await auth_client.get("/api/workflows")).json()) == 2
    # 重名递增
    dup2 = (await auth_client.post(f"/api/workflows/{wf['id']}/duplicate")).json()
    assert dup2["name"] == "原流 副本 2"


async def test_duplicate_workflow_foreign_404(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "私有"})).json()
    await auth_client.post("/api/auth/login", json={"username": "thief"})
    assert (await auth_client.post(f"/api/workflows/{wf['id']}/duplicate")).status_code == 404
