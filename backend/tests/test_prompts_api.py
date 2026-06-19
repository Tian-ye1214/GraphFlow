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
