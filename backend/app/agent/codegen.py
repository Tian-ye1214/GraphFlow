"""智能处理操作的代码生成：临时单 Agent（零工具、零历史、请求级生命周期）+ 仅采上游列名（不跑数、不预览）。"""
import json

from app.agent.factory import create_agent
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Workflow

INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
只输出一个 JSON 对象，不要任何解释或 markdown 围栏，形如：
{"code": "<Python 源码字符串>", "output_columns": ["<本次新增的列名>", ...]}
code 字段要求：
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas（可 import pandas as pd）；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。
output_columns 字段：列出 code 相对输入新增/产出的列名（仅新增的，没有则空数组 []）。

只给出上游可用列名（不含真实数据），请据指令与列名编写代码。
常见模式（按需选用、灵活组合，最后都 return 行字典列表，如 df.to_dict('records')）：
- 全局/多列复合去重：df.drop_duplicates(subset=[列...])（subset 含 'session' 即按 session 与其它列联合去重）。
- 分组内复杂处理（先按 session 分组、再对每组单独处理）：df.groupby('session', group_keys=False).apply(fn)。
- 过滤/改列：用 pandas 布尔索引或列表推导。"""


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _user_prompt(instruction: str, columns: list[str]) -> str:
    cols = "、".join(columns) if columns else "（未知，按指令中提到的列名处理）"
    return f"用户指令：{instruction}\n\n上游可用列：{cols}"


async def generate_code(model, instruction: str, columns: list[str]) -> dict:
    """按指令+上游列名生成 {code, output_columns}；不执行、不预览。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, columns))
    data = json.loads(strip_code_fences(str(result.output or "")))
    return {"code": data.get("code", ""), "output_columns": data.get("output_columns", [])}


NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": """你为「LLM 合成」节点写配置：根据用户指令和上游可用列，写一段生成提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 形如 {"system_prompt": "...", "user_prompt": "...", "output_column": "..."}。
- user_prompt 用 {{列名}} 引用上游的可用列。""",
    "qc": """你为「质检」节点写判定配置：根据用户指令和上游可用列，写一段判定提示词。
硬性要求：
- 只输出一个 JSON 对象，不要解释或 markdown 围栏。
- 形如 {"system_prompt": "...", "user_prompt": "..."}。
- 提示词要引导模型只输出 {"pass": true|false, "reason": "<不通过原因>"}。
- user_prompt 用 {{列名}} 引用上游的可用列。""",
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
