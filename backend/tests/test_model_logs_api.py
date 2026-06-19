from sqlalchemy import func, select

from app.models import ModelCallLog, Run, Workflow, WorkflowVersion


async def _seed(session_factory, **kw):
    async with session_factory() as s:
        s.add(ModelCallLog(request_json='[{"role":"user","content":"q"}]',
                           response_json="a", model_name="m", provider="openai", **kw))
        await s.commit()


async def _run(session_factory, uid, status="completed"):
    async with session_factory() as s:
        wf = Workflow(user_id=uid, name="w"); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}"); s.add(ver); await s.flush()
        run = Run(user_id=uid, workflow_id=wf.id, workflow_version_id=ver.id, status=status)
        s.add(run); await s.commit()
        return run.id


async def test_list_model_logs_isolated_and_filtered(auth_client, session_factory):
    me = (await auth_client.get("/api/me")).json()["id"]
    await _seed(session_factory, user_id=me, source="synth", run_id=5, node_id="ls")
    await _seed(session_factory, user_id=me, source="redlotus", session_id=3)
    await _seed(session_factory, user_id=999999, source="synth", run_id=5)  # 他人
    r = await auth_client.get("/api/model-logs")
    assert r.status_code == 200
    assert len(r.json()) == 2                       # 不含他人
    r2 = await auth_client.get("/api/model-logs?source=synth")
    assert [x["source"] for x in r2.json()] == ["synth"]
    assert r2.json()[0]["request"] == [{"role": "user", "content": "q"}]


async def test_list_model_logs_negative_limit_capped(auth_client, session_factory):
    """limit=-1 不应绕过上限返回全部行（SQLite LIMIT -1 = 无限）：负数钳为非负。"""
    me = (await auth_client.get("/api/me")).json()["id"]
    for _ in range(3):
        await _seed(session_factory, user_id=me, source="synth")
    r = await auth_client.get("/api/model-logs?limit=-1")
    assert r.status_code == 200 and len(r.json()) == 0   # 负数钳为 0，不再返回全部
    assert len((await auth_client.get("/api/model-logs?limit=2")).json()) == 2   # 回归：正常 limit 生效


async def test_run_model_logs_scoped(auth_client, session_factory):
    me = (await auth_client.get("/api/me")).json()["id"]
    rid = await _run(session_factory, me)
    await _seed(session_factory, user_id=me, source="synth", run_id=rid, node_id="ls")
    r = await auth_client.get(f"/api/runs/{rid}/model-logs")
    assert r.status_code == 200 and len(r.json()) == 1


async def test_delete_run_cascades_model_logs(auth_client, session_factory):
    me = (await auth_client.get("/api/me")).json()["id"]
    rid = await _run(session_factory, me)
    await _seed(session_factory, user_id=me, source="synth", run_id=rid)
    await auth_client.delete(f"/api/runs/{rid}")
    async with session_factory() as s:
        n = await s.scalar(select(func.count()).select_from(ModelCallLog).where(ModelCallLog.run_id == rid))
    assert n == 0
