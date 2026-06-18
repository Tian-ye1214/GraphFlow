import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from app.agent import codegen
from app.models import Dataset, DatasetRow, Workflow

GOOD = "def process(rows):\n    return [{**r, 'ok': True} for r in rows]"


def test_strip_code_fences():
    assert codegen.strip_code_fences(f"```python\n{GOOD}\n```") == GOOD
    assert codegen.strip_code_fences(GOOD) == GOOD


async def test_generate_code_returns_code_and_columns():
    """模型返回 JSON {code, output_columns}，generate_code 解析为 dict，不试跑、不预览。"""
    payload = json.dumps({"code": GOOD, "output_columns": ["ok"]})
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(payload)]))
    out = await codegen.generate_code(model, "加 ok 列", [])
    assert out == {"code": GOOD, "output_columns": ["ok"]}


async def test_generate_code_strips_fences_and_passes_columns():
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[-1].parts[-1].content
        payload = json.dumps({"code": GOOD, "output_columns": []})
        return ModelResponse(parts=[TextPart(f"```json\n{payload}\n```")])

    out = await codegen.generate_code(FunctionModel(fn), "去重", ["q", "category"])
    assert out["code"] == GOOD and out["output_columns"] == []
    # 上游列名进入 prompt，真实行值不进入
    assert "q" in seen["prompt"] and "category" in seen["prompt"]


def _graph(dataset_id: int) -> str:
    return json.dumps({
        "nodes": [{"id": "input_1", "type": "input", "config": {"dataset_ids": [dataset_id]}},
                  {"id": "auto_process_1", "type": "auto_process", "config": {}}],
        "edges": [{"source": "input_1", "target": "auto_process_1"}]})


async def test_columns_from_dataset_fallback(client, session_factory):
    async with session_factory() as s:
        ds = Dataset(user_id=1, name="d", columns_json=json.dumps(["q", "category"]))
        s.add(ds)
        await s.commit()
        s.add_all([DatasetRow(dataset_id=ds.id, idx=i, data_json=json.dumps({"q": i, "category": "x"}))
                   for i in range(8)])
        wf = Workflow(user_id=1, name="w", graph_json=_graph(ds.id))
        s.add(wf)
        await s.commit()
        cols, source = await codegen.gather_upstream_columns(s, wf.id, "auto_process_1", user_id=1)
    assert source == "computed" and cols == ["q", "category"]


async def test_columns_propagate_through_llm(client, session_factory):
    """静态传播能看到上游 llm_synth 声明的 json 输出列（如 q_en），无需任何历史运行。"""
    async with session_factory() as s:
        ds = Dataset(user_id=1, name="d", columns_json=json.dumps(["q"]))
        s.add(ds)
        await s.commit()
        graph = {"nodes": [
            {"id": "input_1", "type": "input", "config": {"dataset_ids": [ds.id]}},
            {"id": "llm_1", "type": "llm_synth",
             "config": {"output_mode": "json", "output_columns": ["q_en"]}},
            {"id": "auto_process_1", "type": "auto_process", "config": {}}],
            "edges": [{"source": "input_1", "target": "llm_1"},
                      {"source": "llm_1", "target": "auto_process_1"}]}
        wf = Workflow(user_id=1, name="w", graph_json=json.dumps(graph))
        s.add(wf)
        await s.commit()
        cols, source = await codegen.gather_upstream_columns(s, wf.id, "auto_process_1", user_id=1)
    assert source == "computed" and cols == ["q", "q_en"]


async def test_columns_none_when_node_missing(client, session_factory):
    async with session_factory() as s:
        wf = Workflow(user_id=1, name="w")
        s.add(wf)
        await s.commit()
        cols, source = await codegen.gather_upstream_columns(s, wf.id, "不存在的节点", user_id=1)
    assert source == "none" and cols == []


async def test_columns_skip_foreign_dataset(client, session_factory):
    """攻击者(user2)的工作流图引用他人(user1)私有数据集 id，取列必须不泄露其列名。"""
    async with session_factory() as s:
        victim = Dataset(user_id=1, name="私有", columns_json=json.dumps(["secret"]))
        s.add(victim)
        await s.commit()
        s.add_all([DatasetRow(dataset_id=victim.id, idx=i, data_json=json.dumps({"secret": i}))
                   for i in range(8)])
        wf = Workflow(user_id=2, name="attacker", graph_json=_graph(victim.id))
        s.add(wf)
        await s.commit()
        cols, source = await codegen.gather_upstream_columns(s, wf.id, "auto_process_1", user_id=2)
    assert source == "none" and cols == []


async def test_generate_node_config_llm_synth():
    out = json.dumps({"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"},
                     ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"```json\n{out}\n```")]))
    cfg = await codegen.generate_node_config(model, "llm_synth", "把 q 翻译成英文", ["q"])
    assert cfg == {"system_prompt": "你是翻译", "user_prompt": "翻译:{{q}}", "output_column": "q_en"}


async def test_generate_node_config_llm_synth_json_mode():
    out = json.dumps({"system_prompt": "你是翻译", "user_prompt": "翻译 {{q}} {{category}}",
                      "output_mode": "json", "output_columns": ["q_en", "category_en"]},
                     ensure_ascii=False)
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(out)]))
    cfg = await codegen.generate_node_config(model, "llm_synth", "把 q、category 翻译成英文拆两列", ["q", "category"])
    assert cfg["output_mode"] == "json" and cfg["output_columns"] == ["q_en", "category_en"]


async def test_generate_node_config_rejects_unknown_type():
    import pytest
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("{}")]))
    with pytest.raises(KeyError):
        await codegen.generate_node_config(model, "input", "x", [])


def test_instructions_guide_grouped_dedup():
    from app.agent.codegen import INSTRUCTIONS
    assert "def process(rows: list[dict]) -> list[dict]" in INSTRUCTIONS  # 核心契约未被改没
    assert "pandas" in INSTRUCTIONS
    assert "groupby" in INSTRUCTIONS  # 分组处理示例在位
    assert "分组" in INSTRUCTIONS
    assert "上游可用列" in INSTRUCTIONS  # 改为按列名生成
    assert "output_columns" in INSTRUCTIONS  # 要求声明产出列
    assert "完整" in INSTRUCTIONS or "全部" in INSTRUCTIONS  # 产出列=运行后完整 schema（替换语义契约）
