from sqlalchemy import func, select

from app.config import settings


async def _login_admin(client, monkeypatch, name="boss"):
    monkeypatch.setattr(settings, "admin_users", name)
    return (await client.post("/api/auth/login", json={"username": name})).json()


async def test_non_admin_forbidden(client):
    await client.post("/api/auth/login", json={"username": "pleb"})
    assert (await client.get("/api/admin/users")).status_code == 403


async def test_list_and_create_users(client, monkeypatch):
    await _login_admin(client, monkeypatch)
    r = await client.post("/api/admin/users", json={"username": "alice"})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    assert (await client.post("/api/admin/users", json={"username": "alice"})).status_code == 422
    users = (await client.get("/api/admin/users")).json()
    assert {u["username"] for u in users} >= {"boss", "alice"}


async def test_act_as_lets_admin_operate_as_user(client, monkeypatch, session_factory):
    from app.models import Dataset
    await _login_admin(client, monkeypatch)
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    assert (await client.get("/api/me")).json()["username"] == "alice"
    files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
    await client.post("/api/datasets/upload", files=files)
    await client.post("/api/admin/act-as", json={"user_id": None})
    assert (await client.get("/api/me")).json()["username"] == "boss"
    async with session_factory() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(Dataset).where(Dataset.user_id == alice["id"]))).scalar()
    assert cnt == 1  # 数据集归属被切换的 alice


async def test_delete_user_cascade(client, monkeypatch, session_factory):
    from app.models import Dataset, User
    await _login_admin(client, monkeypatch)
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    files = [("files", ("a.jsonl", b'{"q": 1}\n', "application/octet-stream"))]
    await client.post("/api/datasets/upload", files=files)
    await client.post("/api/admin/act-as", json={"user_id": None})
    assert (await client.delete(f"/api/admin/users/{alice['id']}")).status_code == 200
    async with session_factory() as s:
        assert (await s.execute(
            select(func.count()).select_from(User).where(User.id == alice["id"]))).scalar() == 0
        assert (await s.execute(
            select(func.count()).select_from(Dataset).where(Dataset.user_id == alice["id"]))).scalar() == 0


async def test_cannot_delete_self(client, monkeypatch):
    admin = await _login_admin(client, monkeypatch)
    assert (await client.delete(f"/api/admin/users/{admin['id']}")).status_code == 409
