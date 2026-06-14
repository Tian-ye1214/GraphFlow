import json
from sqlalchemy import select
from app.models import (User, Workflow, WorkflowVersion, Run, RunRow, RunLog,
                        QcMetric, AgentSession, AgentMessage)


async def _mk_run(s, uid, wf_id, ver_id, status):
    r = Run(user_id=uid, workflow_id=wf_id, workflow_version_id=ver_id, status=status)
    s.add(r); await s.flush()
    s.add(RunRow(run_id=r.id, node_id="n", row_idx=0, status="done"))
    s.add(RunLog(run_id=r.id, message="x"))
    s.add(QcMetric(run_id=r.id, node_id="qc", total=1, first_round_pass=1))
    return r.id


async def test_delete_all_runs_skips_active_and_scopes_user(auth_client, session_factory):
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        other = User(username="other"); s.add(other); await s.flush()
        wf = Workflow(user_id=uid, name="w"); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}"); s.add(ver); await s.flush()
        done_id = await _mk_run(s, uid, wf.id, ver.id, "completed")
        running_id = await _mk_run(s, uid, wf.id, ver.id, "running")
        owf = Workflow(user_id=other.id, name="ow"); s.add(owf); await s.flush()
        over = WorkflowVersion(workflow_id=owf.id, version=1, graph_json="{}"); s.add(over); await s.flush()
        other_id = await _mk_run(s, other.id, owf.id, over.id, "completed")
        await s.commit()
    r = await auth_client.delete("/api/runs")
    assert r.status_code == 200 and r.json()["deleted"] == 1
    async with session_factory() as s:
        ids = {x for (x,) in (await s.execute(select(Run.id))).all()}
        rows = (await s.execute(select(RunRow).where(RunRow.run_id == done_id))).scalars().all()
    assert done_id not in ids and running_id in ids and other_id in ids
    assert rows == []


async def test_delete_all_sessions(auth_client, session_factory):
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        for _ in range(2):
            sess = AgentSession(user_id=uid, models_json=json.dumps({"coordinator": 1}))
            s.add(sess); await s.flush()
            s.add(AgentMessage(session_id=sess.id, role="user", content_json="{}"))
        other = User(username="other2"); s.add(other); await s.flush()
        osess = AgentSession(user_id=other.id, models_json="{}"); s.add(osess); await s.flush()
        await s.commit(); other_sid = osess.id
    r = await auth_client.delete("/api/agent/sessions")
    assert r.status_code == 200 and r.json()["deleted"] == 2
    async with session_factory() as s:
        remaining = {x for (x,) in (await s.execute(select(AgentSession.id))).all()}
    assert remaining == {other_sid}
