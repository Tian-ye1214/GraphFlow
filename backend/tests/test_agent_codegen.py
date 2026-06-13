import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from app.agent import codegen
from app.models import Dataset, DatasetRow, Run, RunRow, Workflow, WorkflowVersion

GOOD = "def process(rows):\n    return [{**r, 'ok': True} for r in rows]"
BAD = "def process(rows):\n    raise RuntimeError('炸了')"


def test_strip_code_fences():
    assert codegen.strip_code_fences(f"```python\n{GOOD}\n```") == GOOD
    assert codegen.strip_code_fences(GOOD) == GOOD


async def test_generate_no_sample_skips_preview():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(GOOD)]))
    code, preview, error = await codegen.generate_with_repair(model, "加 ok 列", [])
    assert code == GOOD and preview is None and error is None


async def test_generate_with_preview():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```python\n{GOOD}\n```")]))
    code, preview, error = await codegen.generate_with_repair(model, "加 ok 列", [{"a": 1}])
    assert error is None and preview == [{"a": 1, "ok": True}]


async def test_repair_loop_fixes_bad_code():
    calls = []

    def fn(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart(BAD if len(calls) == 1 else GOOD)])

    code, preview, error = await codegen.generate_with_repair(FunctionModel(fn), "x", [{"a": 1}])
    assert len(calls) == 2 and error is None and preview == [{"a": 1, "ok": True}]


async def test_repair_exhausted_returns_error():
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(BAD)]))
    code, preview, error = await codegen.generate_with_repair(model, "x", [{"a": 1}])
    assert preview is None and "炸了" in error


def _graph(dataset_id: int) -> str:
    return json.dumps({
        "nodes": [{"id": "input_1", "type": "input", "config": {"dataset_ids": [dataset_id]}},
                  {"id": "auto_process_1", "type": "auto_process", "config": {}}],
        "edges": [{"source": "input_1", "target": "auto_process_1"}]})


async def test_sample_from_dataset_fallback(client, session_factory):
    async with session_factory() as s:
        ds = Dataset(user_id=1, name="d")
        s.add(ds)
        await s.commit()
        s.add_all([DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": i}))
                   for i in range(8)])
        wf = Workflow(user_id=1, name="w", graph_json=_graph(ds.id))
        s.add(wf)
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "auto_process_1", user_id=1)
    assert source == "dataset" and len(rows) == 5 and rows[0] == {"q": 0}


async def test_sample_prefers_last_run(client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=1, name="w", graph_json=_graph(999))
        s.add(wf)
        await s.commit()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=_graph(999))
        s.add(ver)
        await s.commit()
        run = Run(user_id=1, workflow_id=wf.id, workflow_version_id=ver.id, status="completed")
        s.add(run)
        await s.commit()
        s.add(RunRow(run_id=run.id, node_id="input_1", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "来自上次运行"}])))
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "auto_process_1", user_id=1)
    assert source == "last_run" and rows == [{"q": "来自上次运行"}]


async def test_sample_none_when_node_missing(client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=1, name="w")
        s.add(wf)
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "不存在的节点", user_id=1)
    assert source == "none" and rows == []


async def test_sample_skips_foreign_dataset(client, session_factory):
    """攻击者(user2)的工作流图引用他人(user1)私有数据集 id，取样必须不泄露其行。"""
    async with session_factory() as s:
        victim = Dataset(user_id=1, name="私有")
        s.add(victim)
        await s.commit()
        s.add_all([DatasetRow(dataset_id=victim.id, idx=i, data_json=json.dumps({"secret": i}))
                   for i in range(8)])
        wf = Workflow(user_id=2, name="attacker", graph_json=_graph(victim.id))
        s.add(wf)
        await s.commit()
        rows, source = await codegen.gather_sample_rows(s, wf.id, "auto_process_1", user_id=2)
    assert source == "none" and rows == []


async def test_generate_node_config_llm_synth():
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    out = json.dumps({"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"},
                     ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```json\n{out}\n```")]))
    cfg = await codegen.generate_node_config(model, "llm_synth", "把 q 翻译成英文", [{"q": "你好"}])
    assert cfg == {"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}


async def test_generate_node_config_rejects_unknown_type():
    import pytest
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, TextPart
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("{}")]))
    with pytest.raises(KeyError):
        await codegen.generate_node_config(model, "input", "x", [])
