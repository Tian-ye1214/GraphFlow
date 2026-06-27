import json
from app.agent.run_tools import RunToolkit
from app.models import (ModelCallLog, QcMetric, Run, RunNodeState, RunRow, User, Workflow,
                        WorkflowVersion)


async def _seed_run(sf):
    async with sf() as s:
        u = User(username="rt"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="W", graph_json='{"nodes":[],"edges":[]}')
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json='{"nodes":[],"edges":[]}')
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.flush()
        s.add(RunRow(run_id=run.id, node_id="o", row_idx=0, status="done", data_json='{"ans":"x"}'))
        s.add(RunNodeState(run_id=run.id, node_id="o", status="done", total=1, done=1, failed=0))
        await s.commit()
        return u.id, wf.id, run.id


async def test_list_runs(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    out = json.loads(await RunToolkit(sf, uid).list_runs())
    assert any(r["id"] == run_id and r["workflow_name"] == "W" for r in out["rows"])


async def test_get_run_and_rows(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    detail = json.loads(await RunToolkit(sf, uid).get_run(run_id))
    assert detail["status"] == "completed"
    rows = json.loads(await RunToolkit(sf, uid).read_run_rows(run_id, "o"))
    assert rows["rows"][0]["data"]["ans"] == "x"


async def test_run_reads_cross_tenant(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    out = json.loads(await RunToolkit(sf, uid + 999).get_run(run_id))
    assert out.get("error") == "run_not_found"
