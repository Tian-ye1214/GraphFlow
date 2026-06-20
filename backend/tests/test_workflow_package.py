import io
import json
import zipfile

import pytest

import app.services.workflow_package as wp
from app.engine.graph import parse_graph
from app.models import (Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, User, Workflow)


# ---------------- Task 1: 纯函数 ----------------

def test_collect_refs_gathers_all_kinds_and_skips_dirty():
    graph = parse_graph({"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [3, 5, "x", True]}},
        {"id": "g", "type": "llm_synth", "config": {"model_config_id": 1, "system_prompt_ref": 7}},
        {"id": "q", "type": "qc", "config": {"judge_model_ids": [1, 2], "user_prompt_ref": 7}},
        {"id": "bad", "type": "llm_synth", "config": "not-a-dict"},
    ], "edges": []})
    ds, models, prompts = wp.collect_refs(graph)
    assert ds == {3, 5}            # "x"/True(bool) 被跳过
    assert models == {1, 2}
    assert prompts == {7}


def test_redact_headers_only_sensitive_literal_values():
    g = {"nodes": [
        {"id": "h", "type": "http_fetch", "config": {"headers": {
            "Authorization": "Bearer sk-secret", "X-Api-Key": "abc",
            "Content-Type": "application/json", "X-Token": "{{tok}}"}}},
        {"id": "x", "type": "input", "config": {}},
    ], "edges": []}
    red = wp.redact_headers(g)
    h = g["nodes"][0]["config"]["headers"]
    assert h["Authorization"] == wp.REDACTED
    assert h["X-Api-Key"] == wp.REDACTED
    assert h["Content-Type"] == "application/json"   # 非敏感头保留
    assert h["X-Token"] == "{{tok}}"                 # 模板值放行
    assert {(r["node_id"], r["header"]) for r in red} == {("h", "Authorization"), ("h", "X-Api-Key")}


# ---------------- Task 2: 导出 ----------------

async def _seed_workflow(session_factory):
    """建 1 用户 + 1 模型(带 key) + 1 提示词(2 版) + 1 数据集(2 行) + 1 引用它们的工作流。
    返回 (uid, wf_id)。"""
    from app.crypto import encrypt
    from app.models import (Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, User,
                            Workflow)
    async with session_factory() as s:
        u = User(username="exp"); s.add(u); await s.flush()
        m = ModelConfig(user_id=u.id, name="m1", model_name="deepseek", base_url="http://x",
                        api_key_enc=encrypt("SECRET-KEY"), default_params_json='{"temperature": 0}')
        p = Prompt(user_id=u.id, name="p1", description="d"); s.add_all([m, p]); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="旧", variables_json="[]"))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="新正文", variables_json='["q"]'))
        d = Dataset(user_id=u.id, name="ds1", row_count=2, columns_json='["q"]'); s.add(d); await s.flush()
        s.add(DatasetRow(dataset_id=d.id, idx=0, data_json='{"q": "007"}'))
        s.add(DatasetRow(dataset_id=d.id, idx=1, data_json='{"q": "你好"}'))
        graph = {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [d.id]}},
            {"id": "g", "type": "llm_synth",
             "config": {"model_config_id": m.id, "system_prompt_ref": p.id}},
            {"id": "h", "type": "http_fetch",
             "config": {"headers": {"Authorization": "Bearer sk-x", "Accept": "*/*"}}},
        ], "edges": [{"source": "in", "target": "g", "kind": "normal"}]}
        wf = Workflow(user_id=u.id, name="链路A", graph_json=json.dumps(graph, ensure_ascii=False))
        s.add(wf); await s.commit()
        return u.id, wf.id


async def test_export_package_self_contained_no_key_redacted(session_factory, tmp_path):
    _uid, wf_id = await _seed_workflow(session_factory)
    dest = tmp_path / "out.gfpkg"
    async with session_factory() as s:
        wf = await s.get(Workflow, wf_id)
        await wp.export_package(s, wf, str(dest))
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        ds_lines = zf.read("datasets/%d.jsonl" % manifest["datasets"][0]["id"]).decode().splitlines()
    assert manifest["kind"] == wp.PACKAGE_KIND and manifest["schema_version"] == 1
    # 模型不含 key
    assert manifest["models"][0]["name"] == "m1"
    assert "api_key" not in manifest["models"][0] and "api_key_enc" not in manifest["models"][0]
    # 提示词取最新版正文
    assert manifest["prompts"][0]["body"] == "新正文" and manifest["prompts"][0]["variables"] == ["q"]
    # http 头脱敏
    httpn = next(n for n in manifest["workflow"]["graph"]["nodes"] if n["id"] == "h")
    assert httpn["config"]["headers"]["Authorization"] == wp.REDACTED
    assert httpn["config"]["headers"]["Accept"] == "*/*"
    assert manifest["redactions"] == [{"node_id": "h", "header": "Authorization"}]
    # 数据集行类型保真（"007" 仍是字符串）
    assert [json.loads(line) for line in ds_lines] == [{"q": "007"}, {"q": "你好"}]
    # 库内 graph 未被脱敏污染（redact 只改导出副本）
    async with session_factory() as s:
        wf2 = await s.get(Workflow, wf_id)
        h_in_db = next(n for n in json.loads(wf2.graph_json)["nodes"] if n["id"] == "h")
        assert h_in_db["config"]["headers"]["Authorization"] == "Bearer sk-x"

