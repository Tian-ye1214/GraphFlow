"""WorkflowDataPreview 直接单测：A1 真实行数 / A2 预算裁剪。"""
import json

from app.agent.data_preview import MAX_PREVIEW_CHARS, WorkflowDataPreview
from app.models import Run, RunRow, User, Workflow, WorkflowVersion


async def _seed_basic(sf):
    """tester + 一个 wf(in->gen) + 一次 completed run。返回 (uid, wf_id, run_id)。"""
    graph = json.dumps({
        "nodes": [{"id": "in", "type": "input", "config": {}},
                  {"id": "gen", "type": "llm_synth", "config": {}}],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"}]})
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        wf = Workflow(user_id=u.id, name="流", graph_json=graph); s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=graph); s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run); await s.flush()
        ids = (u.id, wf.id, run.id)
        await s.commit()
    return ids


async def test_overview_counts_real_data_rows_not_runrows(session_factory):
    """A1: barrier 节点(1 条 RunRow 装 3 行)的 row_count 应为真实数据行数 3，而非 RunRow 数 1。"""
    sf = session_factory
    uid, wf_id, run_id = await _seed_basic(sf)
    async with sf() as s:
        s.add(RunRow(run_id=run_id, node_id="in", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "a"}, {"q": "b"}, {"q": "c"}])))  # barrier：1 RunRow=3 行
        for i in range(3):   # gen 逐行：3 RunRow 各 1 行
            s.add(RunRow(run_id=run_id, node_id="gen", row_idx=i, status="done",
                         data_json=json.dumps([{"q": "a", "out": f"r{i}"}])))
        await s.commit()
    out = json.loads(await WorkflowDataPreview(sf, uid).preview_workflow_data(wf_id))
    counts = {r["node_id"]: r["row_count"] for r in out["rows"]}
    assert counts["in"] == 3 and counts["gen"] == 3   # in 是 3 不是 1


async def test_wide_table_preview_stays_parseable_under_budget(session_factory):
    """A2: 宽表预览按预算裁剪，输出仍是完整可解析 JSON(≤预算)，并报告 omitted_rows。"""
    sf = session_factory
    uid, wf_id, run_id = await _seed_basic(sf)
    big = "x" * 2000
    async with sf() as s:
        rows = [{f"col{c}": big for c in range(40)} for _ in range(20)]  # 20 行 × 40 大列
        s.add(RunRow(run_id=run_id, node_id="in", row_idx=0, status="done",
                     data_json=json.dumps(rows)))
        await s.commit()
    raw = await WorkflowDataPreview(sf, uid).preview_workflow_data(
        wf_id, node_id="in", source="latest_run", limit=20)
    payload = json.loads(raw)            # 不抛 = 完整可解析
    assert len(raw) <= MAX_PREVIEW_CHARS + 2000   # 受预算约束(留壳余量)
    assert payload["omitted_rows"] > 0 and "hint" in payload
    assert len(payload["rows"]) < 20     # 确实裁了行
