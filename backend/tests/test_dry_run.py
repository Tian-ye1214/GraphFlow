"""dry_run_node 单测：复用引擎纯函数试跑单节点；铁律=零副作用（不落 RunRow/ModelCallLog）。"""
import json

import pytest
from sqlalchemy import func, select

from app.engine.dry_run import (LOCAL_SAMPLE_CAP, MODEL_SAMPLE_CAP, DryRunNotFound, dry_run_node,
                                make_codegen_dry_run_tools, make_dry_run_tools)
from app.models import (Dataset, DatasetRow, ModelCallLog, ModelConfig, Prompt, PromptVersion,
                        RunRow, User, Workflow)
from app.services import llm
from app.services.model_log import log_context

LLM_GRAPH = {"nodes": [
    {"id": "in", "type": "input", "config": {"dataset_ids": []}},
    {"id": "gen", "type": "llm_synth", "config": {
        "model_config_id": 0, "system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}",
        "output_column": "a"}},
    {"id": "out", "type": "output", "config": {}}],
    "edges": [{"source": "in", "target": "gen", "kind": "normal"},
              {"source": "gen", "target": "out", "kind": "normal"}]}

QC_GRAPH = {"nodes": [
    {"id": "in", "type": "input", "config": {"dataset_ids": []}},
    {"id": "qc", "type": "qc", "config": {
        "judge_model_ids": [0], "pass_k": 1, "system_prompt": "判定员", "user_prompt": "判:{{q}}"}}],
    "edges": [{"source": "in", "target": "qc", "kind": "normal"}]}

HTTP_GRAPH = {"nodes": [
    {"id": "in", "type": "input", "config": {"dataset_ids": []}},
    {"id": "fetch", "type": "http_fetch", "config": {
        "method": "GET", "url": "http://api/{{q}}", "extract": {"echo": "data.echo"}, "retries": 1}}],
    "edges": [{"source": "in", "target": "fetch", "kind": "normal"}]}

AUTO_GRAPH = {"nodes": [
    {"id": "in", "type": "input", "config": {"dataset_ids": []}},
    {"id": "proc", "type": "auto_process", "config": {"operations": [{"op": "dedup"}]}}],
    "edges": [{"source": "in", "target": "proc", "kind": "normal"}]}


async def _seed(sf, graph: dict, rows: list[dict], *, username="tester"):
    """建 用户+模型+数据集(rows)+工作流(graph)。图里 model_config_id==0/judge_model_ids==[0] 占位换真 id，
    input.dataset_ids 指向新建数据集。返回 (uid, wf_id, mc_id, ds_id)。"""
    async with sf() as s:
        u = User(username=username); s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="qwen", base_url="http://x/v1",
                         api_key_enc="k", default_params_json="{}"); s.add(mc); await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=len(rows),
                     columns_json=json.dumps(sorted({k for r in rows for k in r})))
        s.add(ds); await s.flush()
        for i, r in enumerate(rows):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps(r, ensure_ascii=False)))
        g = json.loads(json.dumps(graph))
        for n in g["nodes"]:
            c = n["config"]
            if n["type"] == "input":
                c["dataset_ids"] = [ds.id]
            if c.get("model_config_id") == 0:
                c["model_config_id"] = mc.id
            if c.get("judge_model_ids") == [0]:
                c["judge_model_ids"] = [mc.id]
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(g, ensure_ascii=False))
        s.add(wf); await s.flush()
        ids = (u.id, wf.id, mc.id, ds.id)
        await s.commit()
    return ids


def _patch_chat(monkeypatch, fn):
    async def fake_chat(mc, system, user, params=None, retries=3):
        return fn(system, user)
    monkeypatch.setattr(llm, "chat", fake_chat)


async def _table_counts(sf):
    async with sf() as s:
        rr = (await s.execute(select(func.count()).select_from(RunRow))).scalar()
        ml = (await s.execute(select(func.count()).select_from(ModelCallLog))).scalar()
    return rr, ml


async def test_dry_run_llm_renders_and_outputs(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "你好"}, {"q": "世界"}])
    _patch_chat(monkeypatch, lambda s, u: (f"[{u}]", {"prompt_tokens": 3, "completion_tokens": 7}))
    out = await dry_run_node(sf, uid, wf_id, "gen")
    assert out["node_type"] == "llm_synth" and out["sample_source"] == "dataset"
    assert out["sampled"] == 2 and len(out["rows"]) == 2
    r0 = out["rows"][0]
    assert r0["rendered_system"] == "你是翻译" and r0["rendered_user"] == "翻译:你好"
    assert r0["output"]["a"] == "[翻译:你好]" and r0["new_columns"] == ["a"]
    assert out["usage"] == {"prompt_tokens": 6, "completion_tokens": 14}


async def test_dry_run_no_side_effects_even_inside_log_context(session_factory, monkeypatch):
    """零副作用：不落 RunRow；即便被 node-assist 的 log_context 包裹也不偷写 ModelCallLog。"""
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 1, "completion_tokens": 1}))
    before = await _table_counts(sf)
    with log_context(user_id=uid, source="assistant"):
        await dry_run_node(sf, uid, wf_id, "gen")
    assert await _table_counts(sf) == before == (0, 0)


async def test_dry_run_render_only_skips_model(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    called = {"n": 0}

    def fn(s, u):
        called["n"] += 1
        return "x", {"prompt_tokens": 1, "completion_tokens": 1}

    _patch_chat(monkeypatch, fn)
    out = await dry_run_node(sf, uid, wf_id, "gen", call_model=False)
    assert called["n"] == 0
    assert out["rows"][0]["rendered_user"] == "翻译:a" and "output" not in out["rows"][0]


async def test_dry_run_reports_missing_cols(session_factory, monkeypatch):
    sf = session_factory
    g = json.loads(json.dumps(LLM_GRAPH))
    g["nodes"][1]["config"]["user_prompt"] = "翻译:{{q}} 风格{{style}}"
    uid, wf_id, _, _ = await _seed(sf, g, [{"q": "a"}])
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    out = await dry_run_node(sf, uid, wf_id, "gen", call_model=False)
    assert out["rows"][0]["missing_cols"] == ["style"]


async def test_dry_run_caps_model_sample(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": str(i)} for i in range(10)])
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    out = await dry_run_node(sf, uid, wf_id, "gen", limit=99)
    assert out["limit"] == MODEL_SAMPLE_CAP and out["sampled"] == MODEL_SAMPLE_CAP


async def test_dry_run_qc_verdict(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, QC_GRAPH, [{"q": "好样本"}])
    _patch_chat(monkeypatch, lambda s, u: (json.dumps({"status": "pass", "reason": "达标"}),
                                           {"prompt_tokens": 2, "completion_tokens": 3}))
    out = await dry_run_node(sf, uid, wf_id, "qc")
    assert out["node_type"] == "qc" and out["pass_k"] == 1
    r0 = out["rows"][0]
    assert r0["passed"] is True and r0["per_model"][0]["status"] == "pass"
    assert r0["rendered_user"] == "判:好样本"


async def test_dry_run_http_get(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, HTTP_GRAPH, [{"q": "北京"}])

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"data": {"echo": "E" + url.rsplit("/", 1)[-1]}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    out = await dry_run_node(sf, uid, wf_id, "fetch")
    r0 = out["rows"][0]
    assert r0["rendered_url"] == "http://api/北京" and r0["method"] == "GET"
    assert r0["output"]["echo"] == "E北京" and r0["new_columns"] == ["echo"]


async def test_dry_run_http_non_get_needs_confirm(session_factory, monkeypatch):
    sf = session_factory
    g = json.loads(json.dumps(HTTP_GRAPH))
    g["nodes"][1]["config"]["method"] = "POST"
    uid, wf_id, _, _ = await _seed(sf, g, [{"q": "x"}])
    called = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        called["n"] += 1
        return 200, json.dumps({"data": {"echo": "e"}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    blocked = await dry_run_node(sf, uid, wf_id, "fetch")
    assert blocked["needs_confirm"] is True and blocked["rows"] == [] and called["n"] == 0
    allowed = await dry_run_node(sf, uid, wf_id, "fetch", allow_side_effects=True)
    assert called["n"] == 1 and allowed["rows"][0]["output"]["echo"] == "e"


async def test_dry_run_auto_process_dedup(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, AUTO_GRAPH,
                                   [{"q": "a"}, {"q": "a"}, {"q": "b"}])
    out = await dry_run_node(sf, uid, wf_id, "proc", limit=10)
    assert out["node_type"] == "auto_process"
    assert [r["q"] for r in out["output_rows"]] == ["a", "b"]
    assert len(out["input_rows"]) == 3


async def test_dry_run_unsupported_type_rejected(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    for nid in ("in", "out"):
        with pytest.raises(ValueError, match="不支持试跑"):
            await dry_run_node(sf, uid, wf_id, nid)


async def test_dry_run_dirty_config_rejected(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    for bad in (0, -1, "x"):
        with pytest.raises(ValueError, match="fanout_n"):
            await dry_run_node(sf, uid, wf_id, "gen", override_config={"fanout_n": bad})


async def test_dry_run_not_found(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    with pytest.raises(DryRunNotFound):
        await dry_run_node(sf, uid, 99999, "gen")
    with pytest.raises(DryRunNotFound):
        await dry_run_node(sf, uid, wf_id, "nope")


async def test_dry_run_rejects_borrowing_other_tenant_model(session_factory, monkeypatch):
    """红线：用 override 借他人模型试跑 → 归属校验拒（点名模型配置）。"""
    sf = session_factory
    a_uid, _, a_mc, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}], username="alice")
    b_uid, b_wf, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "b"}], username="bob")
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    with pytest.raises(ValueError, match="模型配置"):
        await dry_run_node(sf, b_uid, b_wf, "gen", override_config={"model_config_id": a_mc})


async def test_dry_run_resolves_prompt_ref(session_factory, monkeypatch):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    async with sf() as s:
        p = Prompt(user_id=uid, name="译", description=""); s.add(p); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="你是专业译员", variables_json="[]"))
        pid = p.id
        await s.commit()
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    out = await dry_run_node(sf, uid, wf_id, "gen",
                             override_config={"system_prompt_ref": pid}, call_model=False)
    assert out["rows"][0]["rendered_system"] == "你是专业译员"
    with pytest.raises(ValueError, match="不存在"):
        await dry_run_node(sf, uid, wf_id, "gen", override_config={"system_prompt_ref": 999999})


async def test_make_dry_run_tools_uses_draft_config(session_factory, monkeypatch):
    """节点助手工具：用草稿 current_config 试跑；产物为可解析 JSON 串。"""
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    [tool] = make_dry_run_tools(sf, uid, wf_id, "gen", current_config={"user_prompt": "草:{{q}}"})
    out = json.loads(await tool(call_model=False))
    assert out["rows"][0]["rendered_user"] == "草:a"


async def test_make_dry_run_tools_returns_error_json(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, LLM_GRAPH, [{"q": "a"}])
    [tool] = make_dry_run_tools(sf, uid, wf_id, "in")   # input 节点不支持
    assert "error" in json.loads(await tool(call_model=False))


async def test_make_codegen_dry_run_tools_runs_code(session_factory):
    sf = session_factory
    uid, wf_id, _, _ = await _seed(sf, AUTO_GRAPH, [{"q": "a"}, {"q": "b"}])
    [tool] = make_codegen_dry_run_tools(sf, uid, wf_id, "proc")
    code = "def process(rows):\n    return [{**r, 'n': len(r['q'])} for r in rows]\n"
    out = json.loads(await tool(code=code))
    assert out["output_rows"][0]["n"] == 1


async def test_dry_run_empty_sample_notes(session_factory, monkeypatch):
    sf = session_factory
    g = json.loads(json.dumps(LLM_GRAPH))
    async with sf() as s:   # 无数据集、无运行：input.dataset_ids 留空
        u = User(username="empty"); s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="q", base_url="http://x/v1",
                         api_key_enc="k", default_params_json="{}"); s.add(mc); await s.flush()
        g["nodes"][1]["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(g)); s.add(wf); await s.flush()
        uid, wf_id = u.id, wf.id
        await s.commit()
    _patch_chat(monkeypatch, lambda s, u: ("x", {"prompt_tokens": 0, "completion_tokens": 0}))
    out = await dry_run_node(sf, uid, wf_id, "gen")
    assert out["sampled"] == 0 and out["rows"] == [] and "note" in out
