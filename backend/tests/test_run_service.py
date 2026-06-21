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


async def _seed_two_users(session_factory):
    from app.models import Dataset, ModelConfig, User
    async with session_factory() as s:
        a = User(username="a"); b = User(username="b"); s.add_all([a, b]); await s.flush()
        ds_a = Dataset(user_id=a.id, name="dsA", row_count=1, columns_json="[]")
        mc_a = ModelConfig(user_id=a.id, name="mA", base_url="http://x/v1", api_key_enc="k")
        mc_b = ModelConfig(user_id=b.id, name="mB", base_url="http://y/v1", api_key_enc="k")
        s.add_all([ds_a, mc_a, mc_b]); await s.flush()
        ids = (a.id, b.id, ds_a.id, mc_a.id, mc_b.id)
        await s.commit()
    return ids


async def test_ownership_passes_for_own_resources(session_factory):
    import pytest  # noqa
    from app.engine.graph import parse_graph
    aid, _b, dsa, mca, _mcb = await _seed_two_users(session_factory)
    graph = parse_graph({"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [dsa]}},
        {"id": "gen", "type": "llm_synth", "config": {"model_config_id": mca}},
        {"id": "qc", "type": "qc", "config": {"judge_model_ids": [mca]}}], "edges": []})
    async with session_factory() as s:
        await rs.validate_graph_resource_ownership(s, graph, aid)   # 不抛


async def test_ownership_rejects_other_users_resources(session_factory):
    import pytest
    from app.engine.graph import parse_graph
    aid, bid, dsa, _mca, mcb = await _seed_two_users(session_factory)
    # A 借 B 的模型 → 拒
    g_model = parse_graph({"nodes": [{"id": "gen", "type": "llm_synth",
                                      "config": {"model_config_id": mcb}}], "edges": []})
    # A 借 B 的判定模型 → 拒
    g_judge = parse_graph({"nodes": [{"id": "qc", "type": "qc",
                                      "config": {"judge_model_ids": [mcb]}}], "edges": []})
    # B 借 A 的数据集 → 拒
    g_ds = parse_graph({"nodes": [{"id": "in", "type": "input",
                                   "config": {"dataset_ids": [dsa]}}], "edges": []})
    async with session_factory() as s:
        with pytest.raises(ValueError, match="模型配置"):
            await rs.validate_graph_resource_ownership(s, g_model, aid)
        with pytest.raises(ValueError, match="判定模型"):
            await rs.validate_graph_resource_ownership(s, g_judge, aid)
        with pytest.raises(ValueError, match="数据集"):
            await rs.validate_graph_resource_ownership(s, g_ds, bid)
