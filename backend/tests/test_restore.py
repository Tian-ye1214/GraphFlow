import json

from sqlalchemy import select

from app.models import Run, User, Workflow, WorkflowVersion


async def test_restore_run_version_into_workflow(auth_client, session_factory):
    graph_a = {"nodes": [{"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "config": {}}], "edges": []}
    graph_b = {"nodes": [], "edges": []}
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        wf = Workflow(user_id=uid, name="w_restore", graph_json=json.dumps(graph_a))
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(graph_a))
        s.add(ver)
        await s.flush()
        run = Run(user_id=uid, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.commit()
        wf_id, run_id = wf.id, run.id
    # Mutate graph to B in a separate session
    async with session_factory() as s:
        wf2 = await s.get(Workflow, wf_id)
        wf2.graph_json = json.dumps(graph_b)
        await s.commit()

    r = await auth_client.post(f"/api/runs/{run_id}/restore")
    assert r.status_code == 200

    detail = (await auth_client.get(f"/api/workflows/{wf_id}")).json()
    assert detail["graph"] == graph_a


async def test_restore_foreign_run_rejected(auth_client, session_factory):
    """Another user's run must return 404."""
    graph_a = {"nodes": [], "edges": []}
    async with session_factory() as s:
        other = User(username="restore_other")
        s.add(other)
        await s.flush()
        wf = Workflow(user_id=other.id, name="foreign_wf", graph_json=json.dumps(graph_a))
        s.add(wf)
        await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=json.dumps(graph_a))
        s.add(ver)
        await s.flush()
        run = Run(user_id=other.id, workflow_id=wf.id, workflow_version_id=ver.id,
                  status="completed")
        s.add(run)
        await s.commit()
        run_id = run.id

    r = await auth_client.post(f"/api/runs/{run_id}/restore")
    assert r.status_code == 404
