import io
import json
import zipfile

import pytest

import app.services.workflow_package as wp
from app.engine.graph import parse_graph


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
