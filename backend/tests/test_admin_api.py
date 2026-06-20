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


async def test_delete_user_cascades_model_logs_and_prompts(client, monkeypatch, session_factory):
    """删用户须一并清 ModelCallLog(含完整提示词+回复正文) 与 Prompt/PromptVersion；不得波及他人。"""
    from app.models import ModelCallLog, Prompt, PromptVersion, User
    await _login_admin(client, monkeypatch)
    alice = (await client.post("/api/admin/users", json={"username": "alice"})).json()
    bob = (await client.post("/api/admin/users", json={"username": "bob"})).json()
    # alice 建一个工作流：用于测「user_id 未填、靠 workflow_id 兜底」的历史 node-assist 日志分支
    await client.post("/api/admin/act-as", json={"user_id": alice["id"]})
    wf = (await client.post("/api/workflows", json={"name": "w"})).json()
    await client.post("/api/admin/act-as", json={"user_id": None})
    async with session_factory() as s:
        p = Prompt(user_id=alice["id"], name="p")
        s.add(p)
        await s.flush()
        alice_pid = p.id
        s.add(PromptVersion(prompt_id=alice_pid, version=1, body="正文"))
        s.add(ModelCallLog(user_id=alice["id"], source="qc",
                           request_json='[{"role":"user","content":"x"}]', response_json="y"))
        s.add(ModelCallLog(user_id=0, workflow_id=wf["id"], source="assistant"))  # 历史 user_id 未填
        pb = Prompt(user_id=bob["id"], name="pb")
        s.add(pb)
        await s.flush()
        s.add(PromptVersion(prompt_id=pb.id, version=1, body="bob正文"))
        s.add(ModelCallLog(user_id=bob["id"], source="qc"))
        await s.commit()

    assert (await client.delete(f"/api/admin/users/{alice['id']}")).status_code == 200

    async def cnt(model, cond):
        async with session_factory() as s:
            return (await s.execute(select(func.count()).select_from(model).where(cond))).scalar()

    assert await cnt(User, User.id == alice["id"]) == 0
    assert await cnt(ModelCallLog, ModelCallLog.user_id == alice["id"]) == 0
    assert await cnt(ModelCallLog, ModelCallLog.workflow_id == wf["id"]) == 0   # 兜底分支生效
    assert await cnt(Prompt, Prompt.user_id == alice["id"]) == 0
    assert await cnt(PromptVersion, PromptVersion.prompt_id == alice_pid) == 0
    # 旁观者 bob 完好（级联不越界）
    assert await cnt(ModelCallLog, ModelCallLog.user_id == bob["id"]) == 1
    assert await cnt(Prompt, Prompt.user_id == bob["id"]) == 1
