"""智能处理操作的代码生成：临时单 Agent（零工具、零历史、请求级生命周期）+ 仅采上游列名（不跑数、不预览）。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.factory import create_agent
from app.engine.graph import Graph, parse_graph, upstream_ids
from app.models import Dataset, Run, RunRow, Workflow, WorkflowVersion

SAMPLE_N = 5

INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
硬性要求：
- 只输出 Python 源码，不要任何解释或 markdown 围栏。
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas（可 import pandas as pd）；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。

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


async def generate_code(model, instruction: str, columns: list[str]) -> str:
    """只按指令+上游列名生成处理函数源码；不执行、不预览。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, columns))
    return strip_code_fences(str(result.output or ""))


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


async def gather_upstream_columns(s: AsyncSession, workflow_id: int, node_id: str, user_id: int):
    """取上游节点产出的列名（只名不值）。优先最近一次运行的上游输出键，否则上游 input 数据集列。
    返回 (columns, source)。"""
    run = (await s.execute(select(Run).where(Run.workflow_id == workflow_id)
                           .order_by(Run.id.desc()))).scalars().first()
    if run is not None:
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        rows = await _upstream_run_rows(s, run.id, parse_graph(ver.graph_json), node_id)
        if rows:
            return _columns_of(rows), "last_run"
    wf = await s.get(Workflow, workflow_id)
    cols = await _upstream_dataset_columns(s, parse_graph(wf.graph_json), node_id, user_id)
    if cols:
        return cols, "dataset"
    return [], "none"


def _columns_of(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols and not k.startswith("_qc"):
                cols.append(k)
    return cols


async def _upstream_run_rows(s, run_id: int, graph: Graph, node_id: str) -> list[dict]:
    if node_id not in {n.id for n in graph.nodes}:
        return []
    out: list[dict] = []
    for uid in upstream_ids(graph, node_id):
        recs = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == uid, RunRow.status == "done")
            .order_by(RunRow.row_idx).limit(SAMPLE_N))).scalars().all()
        for r in recs:
            out.extend(json.loads(r.data_json))
        if len(out) >= SAMPLE_N:
            break
    return out


async def _upstream_dataset_columns(s, graph: Graph, node_id: str, user_id: int) -> list[str]:
    by_id = {n.id: n for n in graph.nodes}
    if node_id not in by_id:
        return []
    seen: set[str] = set()
    frontier, dataset_ids = [node_id], []
    while frontier:
        for uid in upstream_ids(graph, frontier.pop()):
            if uid in seen:
                continue
            seen.add(uid)
            frontier.append(uid)
            if by_id[uid].type == "input":
                dataset_ids.extend(by_id[uid].config.get("dataset_ids", []))
    cols: list[str] = []
    for ds_id in dataset_ids:
        ds = await s.get(Dataset, ds_id)
        if ds is None or ds.user_id != user_id:   # 资源归属校验（租户隔离）
            continue
        for c in json.loads(ds.columns_json):
            if c not in cols:
                cols.append(c)
    return cols
