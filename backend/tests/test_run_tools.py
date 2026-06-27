import json
from sqlalchemy import func, select
from app.agent.run_tools import RunToolkit
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)


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


async def test_read_run_qc_default_empty_sample(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    async with sf() as s:
        s.add(QcMetric(run_id=run_id, node_id="q", total=3, first_round_pass=2))
        s.add(QcFailure(run_id=run_id, node_id="q"))  # 故意默认空 sample_json="" / reasons_json
        await s.commit()
    out = json.loads(await RunToolkit(sf, uid).read_run_qc(run_id))
    assert out["metrics"][0]["first_round_pass"] == 2
    assert out["failures"][0]["sample"] is None  # or "null" 兜底生效，不抛
    assert out["failures"][0]["reasons"] == []


async def test_read_run_logs(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    async with sf() as s:
        s.add(RunLog(run_id=run_id, node_id="o", level="info", message="hello"))
        await s.commit()
    out = json.loads(await RunToolkit(sf, uid).read_run_logs(run_id))
    assert any(r["message"] == "hello" for r in out["rows"])


async def test_delete_run_requires_confirmation(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid).delete_run(run_id)               # 默认未确认
    assert "确认" in msg
    assert "[confirm_delete]" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is not None                 # 未删


async def test_delete_run_confirmed_cascades(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid, confirm_delete=True).delete_run(run_id)
    assert "已删除" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is None
        cnt = (await s.execute(select(func.count()).select_from(RunRow)
               .where(RunRow.run_id == run_id))).scalar()
        assert cnt == 0


async def test_restore_workflow_requires_confirmation(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid).restore_workflow_from_run(run_id)
    assert "确认" in msg
    assert "[confirm_delete]" in msg


async def test_restore_workflow_confirmed_overwrites_graph(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    snapshot = '{"nodes":[{"id":"a","type":"input"}],"edges":[]}'
    async with sf() as s:
        run = await s.get(Run, run_id)
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        ver.graph_json = snapshot                                   # run 版本与当前图不同
        await s.commit()
    msg = await RunToolkit(sf, uid, confirm_delete=True).restore_workflow_from_run(run_id)
    assert "已把工作流恢复" in msg
    async with sf() as s:
        wf = await s.get(Workflow, wf_id)
        assert wf.graph_json == snapshot                            # 当前图被 run 版本覆盖


async def test_delete_all_runs_requires_confirmation(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid).delete_all_runs()               # 默认未确认
    assert "确认" in msg
    assert "[confirm_delete]" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is not None                 # 未删


async def test_delete_all_runs_confirmed(session_factory):
    sf = session_factory; uid, wf_id, run_id = await _seed_run(sf)
    msg = await RunToolkit(sf, uid, confirm_delete=True).delete_all_runs()
    assert "删除" in msg
    async with sf() as s:
        assert await s.get(Run, run_id) is None
