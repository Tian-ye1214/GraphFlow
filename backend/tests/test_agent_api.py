import json

import pytest

from app.agent import turns
from app.auth import parse_session_cookie


@pytest.fixture
async def mc_id(auth_client):
    r = await auth_client.post("/api/models", json={
        "name": "m1", "model_name": "qwen", "base_url": "http://llm.local/v1", "api_key": "sk"})
    return r.json()["id"]


@pytest.fixture
def no_run(monkeypatch):
    calls = []
    monkeypatch.setattr(turns.turn_manager, "submit",
                        lambda sid, uid, text: calls.append((sid, uid, text)))
    return calls


async def test_create_and_get_session(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mc_id})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["models"] == {"coordinator": mc_id, "manager": mc_id, "worker": mc_id, "compactor": mc_id}
    # cli.json 已生成：server 取自请求 base_url，cookie 可验签回本人
    wd = turns.session_dir("tester", sid)  # 用户名须与 auth_client 登录名一致
    state = json.loads((wd / "cli.json").read_text(encoding="utf-8"))
    assert state["server"] == "http://test"
    assert parse_session_cookie(state["cookie"]) is not None
    r = await auth_client.get("/api/agent/sessions")
    assert [s["id"] for s in r.json()] == [sid]
    r = await auth_client.get(f"/api/agent/sessions/{sid}")
    assert r.json()["messages"] == []


async def test_session_defaults_compactor_to_coordinator(auth_client, session_factory):
    import json
    from sqlalchemy import select
    from app.models import AgentSession, ModelConfig, User
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        mc = ModelConfig(user_id=uid, name="m", base_url="http://x", api_key_enc="")
        s.add(mc); await s.commit(); mid = mc.id
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": mid})
    assert r.status_code == 200
    async with session_factory() as s:
        sess = (await s.execute(select(AgentSession).order_by(AgentSession.id.desc()))).scalars().first()
        models = json.loads(sess.models_json)
    assert models["compactor"] == mid


async def test_session_title_numbered_per_user(client, no_run):
    """会话编号按用户独立：每个用户的未命名会话从"会话 1"起，不共用全局自增 id。"""
    async def mc_for(username):
        await client.post("/api/auth/login", json={"username": username})
        return (await client.post("/api/models", json={
            "name": "m", "model_name": "x", "base_url": "http://l/v1", "api_key": "sk"})).json()["id"]

    a = await mc_for("alice_u")
    t1 = (await client.post("/api/agent/sessions", json={"model_config_id": a})).json()["title"]
    t2 = (await client.post("/api/agent/sessions", json={"model_config_id": a})).json()["title"]
    b = await mc_for("bob_u")  # 切换用户后重新计数
    t3 = (await client.post("/api/agent/sessions", json={"model_config_id": b})).json()["title"]
    assert (t1, t2, t3) == ("会话 1", "会话 2", "会话 1")


async def test_create_session_per_role_models(auth_client, mc_id, no_run):
    r = await auth_client.post("/api/agent/sessions", json={
        "models": {"coordinator": mc_id, "manager": mc_id, "worker": mc_id}})
    assert r.status_code == 200


async def test_create_session_bad_model(auth_client, no_run):
    r = await auth_client.post("/api/agent/sessions", json={"model_config_id": 999})
    assert r.status_code == 422


async def test_message_flow_and_409(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    text = "帮我搭一个翻译流水线，把 q 列翻译成英文并跑起来"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": text})
    assert r.status_code == 200
    assert no_run == [(sid, 1, text)]
    detail = (await auth_client.get(f"/api/agent/sessions/{sid}")).json()
    assert detail["status"] == "running"
    assert detail["title"] == text[:30]
    assert detail["messages"][0]["role"] == "user"
    r = await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "再来"})
    assert r.status_code == 409


async def test_stop_endpoint(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    r = await auth_client.post(f"/api/agent/sessions/{sid}/stop")
    assert r.status_code == 200
    assert sid in turns.turn_manager.stop_flags


async def test_delete_cleans_workdir(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post(f"/api/agent/sessions/{sid}/messages", json={"text": "hi"})
    wd = turns.session_dir("tester", sid)  # 用户名须与 auth_client 登录名一致
    assert wd.exists()
    r = await auth_client.delete(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200
    assert not wd.exists()
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404


async def test_cross_user_isolation(auth_client, mc_id, no_run):
    sid = (await auth_client.post("/api/agent/sessions",
                                  json={"model_config_id": mc_id})).json()["id"]
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get(f"/api/agent/sessions/{sid}")).status_code == 404
    assert (await auth_client.post(f"/api/agent/sessions/{sid}/messages",
                                   json={"text": "x"})).status_code == 404
    assert (await auth_client.get("/api/agent/sessions")).json() == []


async def test_codegen_endpoint(auth_client, mc_id, monkeypatch):
    from app.routers import agent as agent_router

    async def fake(model, instruction, columns):
        assert instruction == "去重"
        return {"code": "def process(rows):\n    return rows", "output_columns": []}

    monkeypatch.setattr(agent_router, "generate_code", fake)
    wid = (await auth_client.post("/api/workflows", json={"name": "w1"})).json()["id"]
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wid, "node_id": "auto_process_1",
        "instruction": "去重", "model_config_id": mc_id})
    assert r.status_code == 200
    body = r.json()
    assert body["code"].startswith("def process") and body["sample_source"] == "none"
    assert body["columns"] == [] and "preview_rows" not in body  # 只回列名，不再有数据预览


async def test_codegen_ownership(auth_client, mc_id):
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": 9999, "node_id": "x", "instruction": "y", "model_config_id": mc_id})
    assert r.status_code == 404
    wid = (await auth_client.post("/api/workflows", json={"name": "w2"})).json()["id"]
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wid, "node_id": "x", "instruction": "y", "model_config_id": 9999})
    assert r.status_code == 422


async def test_node_assist_guards(auth_client, monkeypatch):
    from app.agent import codegen

    async def fake_cfg(model, node_type, instruction, columns):
        return {"system_prompt": "s", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}

    monkeypatch.setattr(codegen, "generate_node_config", fake_cfg)
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "x", "base_url": "http://x", "api_key": "k"})).json()
    # 成功路径
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "llm_synth_1", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": mc["id"]})
    assert r.status_code == 200
    assert r.json()["config"]["output_column"] == "q_en"
    # 不支持的节点类型
    r2 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "input_1", "node_type": "input",
        "instruction": "x", "model_config_id": mc["id"]})
    assert r2.status_code == 422
    # 他人工作流 → 404
    r3 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": 99999, "node_id": "n", "node_type": "qc",
        "instruction": "x", "model_config_id": mc["id"]})
    assert r3.status_code == 404
    # 空指令 → 422
    r4 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "llm_synth_1", "node_type": "llm_synth",
        "instruction": "   ", "model_config_id": mc["id"]})
    assert r4.status_code == 422
    # 不存在/非自己的模型 → 422
    r5 = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "llm_synth_1", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": 999999})
    assert r5.status_code == 422
