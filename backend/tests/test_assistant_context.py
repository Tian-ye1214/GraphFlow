import json

from app.agent import codegen


class _StubResult:
    def __init__(self, output):
        self.output = output


class _StubAgent:
    def __init__(self, captured, output):
        self._captured = captured
        self._output = output

    async def run(self, prompt, message_history=None):
        self._captured["prompt"] = prompt
        return _StubResult(self._output)


def _patch_agent(monkeypatch, captured, output):
    monkeypatch.setattr(codegen, "create_agent",
                        lambda *a, **k: _StubAgent(captured, output))


async def test_generate_code_includes_current_code(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"code": "def process(rows): return rows", "output_columns": ["x"]}')
    await codegen.generate_code(None, "把B列转成B2", ["A", "B"],
                                current_code="def process(rows):\n    # A->A1\n    return rows")
    assert "A->A1" in cap["prompt"]              # 现有代码进了模型提示
    assert "把B列转成B2" in cap["prompt"]        # 新指令也在


async def test_generate_code_empty_current_code_degrades(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"code": "x", "output_columns": []}')
    await codegen.generate_code(None, "做点啥", ["A"], current_code="")
    assert "现有代码" not in cap["prompt"]        # 没有现有代码时不加该段（退化为现状）


async def test_generate_node_config_includes_current_config(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"system_prompt": "s", "user_prompt": "u", "output_mode": "column", "output_column": "B2"}')
    await codegen.generate_node_config(None, "llm_synth", "把B列转成B2", ["A", "B"],
                                       current_config={"user_prompt": "把 {{A}} 翻译成 A1"})
    assert "把 {{A}} 翻译成 A1" in cap["prompt"]  # 现有配置进了模型提示


async def test_generate_node_config_no_current_degrades(monkeypatch):
    cap = {}
    _patch_agent(monkeypatch, cap, '{"system_prompt": "s", "user_prompt": "u"}')
    await codegen.generate_node_config(None, "qc", "判断是否切题", ["q", "a"], current_config=None)
    assert "现有节点配置" not in cap["prompt"]


async def _make_model_and_wf(auth_client, node_id, node_type):
    mc = (await auth_client.post("/api/models", json={
        "name": "m", "model_name": "q", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    wf = (await auth_client.post("/api/workflows", json={"name": "w"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": {
        "nodes": [{"id": node_id, "type": node_type, "config": {}}], "edges": []}})
    return mc, wf


async def test_codegen_endpoint_passes_current_code(auth_client, monkeypatch):
    cap = {}

    async def fake_gen(model, instruction, columns, current_code="", preview_tools=None, params=None):
        cap["current_code"] = current_code
        return {"code": "x", "output_columns": []}

    monkeypatch.setattr("app.routers.agent.generate_code", fake_gen)
    mc, wf = await _make_model_and_wf(auth_client, "ap", "auto_process")
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wf["id"], "node_id": "ap", "instruction": "做点啥",
        "model_config_id": mc["id"], "current_code": "PRIOR_CODE"})
    assert r.status_code == 200
    assert cap["current_code"] == "PRIOR_CODE"


async def test_node_assist_endpoint_passes_current_config(auth_client, monkeypatch):
    cap = {}

    async def fake_cfg(model, node_type, instruction, columns, current_config=None,
                       preview_tools=None, params=None, history=None):
        cap["current_config"] = current_config
        return {"reply": "ok", "config": {"system_prompt": "s", "user_prompt": "u"}}

    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "翻译", "model_config_id": mc["id"],
        "current_config": {"user_prompt": "把 {{A}} 翻译成 A1"}})
    assert r.status_code == 200
    assert cap["current_config"] == {"user_prompt": "把 {{A}} 翻译成 A1"}


_EXPECTED_TOOLS = {"preview_current_node_input", "describe_current_node_input", "show_workflow_graph",
                   "latest_run_summary", "read_node_output", "read_qc_failures", "read_node_model_logs",
                   "list_user_datasets", "list_user_models", "list_prompts", "get_prompt"}


async def test_node_assist_endpoint_wires_full_toolset(auth_client, monkeypatch):
    cap = {}

    async def fake_cfg(model, node_type, instruction, columns, current_config=None,
                       preview_tools=None, params=None, history=None):
        cap["tools"] = {t.__name__ for t in (preview_tools or [])}
        return {"reply": "ok", "config": None}

    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_cfg)
    mc, wf = await _make_model_and_wf(auth_client, "ls", "llm_synth")
    r = await auth_client.post("/api/agent/node-assist", json={
        "workflow_id": wf["id"], "node_id": "ls", "node_type": "llm_synth",
        "instruction": "x", "model_config_id": mc["id"]})
    assert r.status_code == 200
    assert _EXPECTED_TOOLS <= cap["tools"]   # 端点把全套只读工具接给了助手


async def test_codegen_endpoint_wires_full_toolset(auth_client, monkeypatch):
    cap = {}

    async def fake_gen(model, instruction, columns, current_code="", preview_tools=None, params=None):
        cap["tools"] = {t.__name__ for t in (preview_tools or [])}
        return {"code": "x", "output_columns": []}

    monkeypatch.setattr("app.routers.agent.generate_code", fake_gen)
    mc, wf = await _make_model_and_wf(auth_client, "ap", "auto_process")
    r = await auth_client.post("/api/agent/codegen", json={
        "workflow_id": wf["id"], "node_id": "ap", "instruction": "x", "model_config_id": mc["id"]})
    assert r.status_code == 200
    assert _EXPECTED_TOOLS <= cap["tools"]
