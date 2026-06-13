async def test_me_unauthorized(client):
    r = await client.get("/api/me")
    assert r.status_code == 401


async def test_login_then_me(client):
    r = await client.post("/api/auth/login", json={"username": "alice"})
    assert r.status_code == 200
    assert "gf_session" in r.cookies
    me = await client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_login_idempotent(client):
    a = (await client.post("/api/auth/login", json={"username": "alice"})).json()
    b = (await client.post("/api/auth/login", json={"username": "alice"})).json()
    assert a["id"] == b["id"]


async def test_login_rejects_blank(client):
    r = await client.post("/api/auth/login", json={"username": "  "})
    assert r.status_code == 422


async def test_logout(client):
    await client.post("/api/auth/login", json={"username": "tester"})
    assert (await client.get("/api/me")).status_code == 200
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    assert (await client.get("/api/me")).status_code == 401


async def test_admin_flag_set_on_login(client, monkeypatch, session_factory):
    from sqlalchemy import select
    from app.config import settings
    from app.models import User
    monkeypatch.setattr(settings, "admin_users", "boss,root")
    await client.post("/api/auth/login", json={"username": "boss"})
    await client.post("/api/auth/login", json={"username": "pleb"})
    async with session_factory() as s:
        boss = (await s.execute(select(User).where(User.username == "boss"))).scalar_one()
        pleb = (await s.execute(select(User).where(User.username == "pleb"))).scalar_one()
    assert boss.is_admin is True
    assert pleb.is_admin is False
