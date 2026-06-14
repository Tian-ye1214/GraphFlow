"""智能处理操作的代码生成：临时单 Agent（零工具、零历史、请求级生命周期）+ 仅采上游列名（不跑数、不预览）。"""
import json

from app.agent.factory import create_agent
from app.agent.prompts import load_prompt
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Workflow

INSTRUCTIONS = load_prompt("codegen_system.md")


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _user_prompt(instruction: str, columns: list[str]) -> str:
    cols = "、".join(columns) if columns else "（未知，按指令中提到的列名处理）"
    return load_prompt("codegen_user.md").format(instruction=instruction, columns=cols)


async def generate_code(model, instruction: str, columns: list[str]) -> dict:
    """按指令+上游列名生成 {code, output_columns}；不执行、不预览。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, columns))
    data = json.loads(strip_code_fences(str(result.output or "")))
    return {"code": data.get("code", ""), "output_columns": data.get("output_columns", [])}


NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": load_prompt("node_assist_llm_synth.md"),
    "qc": load_prompt("node_assist_qc.md"),
}


async def generate_node_config(model, node_type: str, instruction: str, columns: list[str]) -> dict:
    """临时单 Agent 为指定节点产出配置 JSON（不跑代码，仅生成提示词）。未知 node_type 抛 KeyError。"""
    agent = create_agent(model, [], NODE_ASSIST_INSTRUCTIONS[node_type])
    result = await agent.run(_user_prompt(instruction, columns))
    return json.loads(strip_code_fences(str(result.output or "")))


async def gather_upstream_columns(s, workflow_id: int, node_id: str, user_id: int):
    """静态推算 node_id 的输入列（沿 llm/处理节点传播）。返回 (columns, source)。"""
    wf = await s.get(Workflow, workflow_id)
    if wf is None or wf.user_id != user_id:
        return [], "none"
    graph = parse_graph(wf.graph_json)
    if node_id not in {n.id for n in graph.nodes}:
        return [], "none"
    dataset_cols = await resolve_dataset_cols(s, graph, user_id)
    cols = propagate_columns(graph, dataset_cols)[node_id]["input"]
    return cols, ("computed" if cols else "none")
