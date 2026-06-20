from sqlalchemy import select

import app.services.run_service as rs


def test_parse_threshold():
    assert rs.parse_threshold("把首轮质检通过率提升到 90% 以上") == 0.9
    assert rs.parse_threshold("达到 0.85") == 0.85
    assert rs.parse_threshold("把数据清洗干净") is None


async def test_first_round_rate_aggregates(session_factory):
    from app.models import QcMetric
    async with session_factory() as s:
        s.add(QcMetric(run_id=7, node_id="a", total=10, first_round_pass=6))
        s.add(QcMetric(run_id=7, node_id="b", total=10, first_round_pass=8))
        await s.commit()
    rate = await rs.first_round_rate(session_factory, 7)
    assert abs(rate - 0.7) < 1e-6                 # (6+8)/(10+10)


async def test_first_round_rate_none_when_no_metric(session_factory):
    assert await rs.first_round_rate(session_factory, 999) is None


async def test_purge_run_rows_cascades_all_children_and_versions(session_factory):
    """purge_run_rows 单点：删一批 run 的 6 张子表 + Run 本身 + (可选)版本快照；新增 run 子表只改这里。"""
    from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                            User, Workflow, WorkflowVersion)
    async with session_factory() as s:
        u = User(username="purge"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="w"); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json="{}"); s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id); s.add(run); await s.flush()
        rid, vid = run.id, ver.id
        s.add(RunRow(run_id=rid, node_id="n", row_idx=0, status="done"))
        s.add(RunNodeState(run_id=rid, node_id="n", status="done", total=1, done=1))
        s.add(RunLog(run_id=rid, message="x"))
        s.add(QcMetric(run_id=rid, node_id="qc", total=1, first_round_pass=1))
        s.add(QcFailure(run_id=rid, node_id="qc"))
        s.add(ModelCallLog(run_id=rid, source="synth", node_id="n"))
        await s.commit()
    async with session_factory() as s:
        await rs.purge_run_rows(s, [rid], version_ids=[vid])
        await s.commit()
    async with session_factory() as s:
        for Model in (RunRow, RunNodeState, RunLog, QcMetric, QcFailure, ModelCallLog):
            assert (await s.execute(select(Model).where(Model.run_id == rid))).scalars().all() == []
        assert await s.get(Run, rid) is None
        assert await s.get(WorkflowVersion, vid) is None


async def test_purge_run_rows_empty_is_noop(session_factory):
    async with session_factory() as s:
        await rs.purge_run_rows(s, [])          # 空入参不抛、不发删除语句
        await s.commit()


def test_unlink_run_exports_removes_only_matching(tmp_path):
    exports = tmp_path / "exports"; exports.mkdir()
    keep = exports / "run99_x.jsonl"; keep.write_text("k")
    gone = exports / "run7_a.jsonl"; gone.write_text("g")
    rs.unlink_run_exports([7], tmp_path)
    assert keep.exists() and not gone.exists()
