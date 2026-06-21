"""WorkflowDataPreview 直接单测：A1 真实行数 / A2 预算裁剪。"""
import json

from app.agent.data_preview import MAX_PREVIEW_CHARS, WorkflowDataPreview
from app.models import Dataset, DatasetRow, Run, RunRow, User, Workflow, WorkflowVersion


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
    assert payload.get("cells_truncated_to")   # 单行超宽触发单格收紧兜底


async def _seed_dataset_wf(sf, recs, columns):
    """tester + 一个数据集(input 节点引用它)。返回 (uid, wf_id)。"""
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        ds = Dataset(user_id=u.id, name="集", source="upload", original_filename="集.jsonl",
                     row_count=len(recs), columns_json=json.dumps(columns))
        s.add(ds); await s.flush()
        for i, r in enumerate(recs):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps(r, ensure_ascii=False)))
        graph = json.dumps({"nodes": [{"id": "in", "type": "input",
                                       "config": {"dataset_ids": [ds.id]}}], "edges": []})
        wf = Workflow(user_id=u.id, name="流", graph_json=graph); s.add(wf); await s.flush()
        ids = (u.id, wf.id)
        await s.commit()
    return ids


async def test_describe_reports_dtypes_missing_and_value_dist(session_factory):
    """describe: 总行数(数据集精确) + 每列 dtype 分布/缺失率/低基数值分布。"""
    sf = session_factory
    uid, wf_id = await _seed_dataset_wf(sf, [
        {"cat": "A", "score": 1, "q": "问1"}, {"cat": "B", "score": 2, "q": "问2"},
        {"cat": "A", "score": None, "q": "问3"}, {"cat": "A", "score": 4, "q": "问4"}],
        ["cat", "score", "q"])
    out = json.loads(await WorkflowDataPreview(sf, uid).describe(wf_id, source="dataset"))
    assert out["total_rows"] == 4 and out["sampled_rows"] == 4 and out["column_count"] == 3
    cols = {c["name"]: c for c in out["columns"]}
    assert cols["cat"]["value_counts"]['"A"'] == 3   # 低基数值分布
    assert cols["score"]["missing_pct"] == 25         # None 计缺失
    assert cols["score"]["dtypes"].get("int") == 3


async def test_describe_high_cardinality_distinct_estimate(session_factory):
    """高基数列(>15 distinct)走 distinct_estimate 分支，不出 value_counts。"""
    sf = session_factory
    uid, wf_id = await _seed_dataset_wf(sf, [{"q": f"问题{i}"} for i in range(20)], ["q"])
    out = json.loads(await WorkflowDataPreview(sf, uid).describe(wf_id, source="dataset", sample_limit=20))
    col = out["columns"][0]
    assert col["distinct_estimate"] == 20 and "value_counts" not in col


async def test_preview_invalid_source_errors(session_factory):
    sf = session_factory
    uid, wf_id = await _seed_dataset_wf(sf, [{"q": "a"}], ["q"])
    out = json.loads(await WorkflowDataPreview(sf, uid).preview_workflow_data(wf_id, source="bogus"))
    assert out["error"] == "invalid_source"


async def test_preview_auto_falls_back_to_dataset_when_no_run(session_factory):
    sf = session_factory
    uid, wf_id = await _seed_dataset_wf(sf, [{"q": "甲"}, {"q": "乙"}], ["q"])
    out = json.loads(await WorkflowDataPreview(sf, uid).preview_workflow_data(wf_id, source="auto"))
    assert out["source"] == "dataset" and out["rows"][0]["q"] == "甲"
