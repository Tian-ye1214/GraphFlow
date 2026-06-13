"""智能处理操作的代码生成：临时单 Agent（零工具、零历史、请求级生命周期）+ 试跑修复循环 + 上游样本采集。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.factory import create_agent
from app.engine.graph import Graph, parse_graph, upstream_ids
from app.engine.pycode import run_process_code
from app.models import DatasetRow, Run, RunRow, Workflow, WorkflowVersion

SAMPLE_N = 5
MAX_REPAIR_ROUNDS = 3

INSTRUCTIONS = """你是数据处理代码生成器，为表格行数据按用户指令写一个 Python 处理函数。
硬性要求：
- 只输出 Python 源码，不要任何解释或 markdown 围栏。
- 必须定义 def process(rows: list[dict]) -> list[dict]，输入输出都是行字典列表。
- 只能用标准库与 pandas；禁止网络访问、禁止读写文件、禁止 exec/eval。
- 数据问题（如列不存在）让代码自然报错，不要静默吞掉。"""


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _user_prompt(instruction: str, sample_rows: list[dict]) -> str:
    sample = (json.dumps(sample_rows, ensure_ascii=False) if sample_rows
              else "（无样本，按指令中提到的列名处理）")
    return f"用户指令：{instruction}\n\n样本行：\n{sample}"


async def generate_with_repair(model, instruction: str, sample_rows: list[dict]):
    """返回 (code, preview_rows|None, error|None)。有样本时试跑并自动修复，最多 MAX_REPAIR_ROUNDS 轮。"""
    agent = create_agent(model, [], INSTRUCTIONS)
    result = await agent.run(_user_prompt(instruction, sample_rows))
    code = strip_code_fences(str(result.output or ""))
    if not sample_rows:
        return code, None, None
    history = result.all_messages()
    for _ in range(MAX_REPAIR_ROUNDS):
        try:
            return code, await run_process_code(code, sample_rows), None
        except ValueError as e:
            result = await agent.run(f"试跑报错，修复后重新输出完整源码：\n{e}",
                                     message_history=history)
            history = result.all_messages()
            code = strip_code_fences(str(result.output or ""))
    try:
        return code, await run_process_code(code, sample_rows), None
    except ValueError as e:
        return code, None, str(e)


async def gather_sample_rows(s: AsyncSession, workflow_id: int, node_id: str):
    """按优先级取样本：最近一次运行的上游输出 → 上游 input 数据集头部 → 无。返回 (rows, source)。"""
    run = (await s.execute(select(Run).where(Run.workflow_id == workflow_id)
                           .order_by(Run.id.desc()))).scalars().first()
    if run is not None:
        ver = await s.get(WorkflowVersion, run.workflow_version_id)
        rows = await _upstream_run_rows(s, run.id, parse_graph(ver.graph_json), node_id)
        if rows:
            return rows[:SAMPLE_N], "last_run"
    wf = await s.get(Workflow, workflow_id)
    rows = await _upstream_dataset_rows(s, parse_graph(wf.graph_json), node_id)
    if rows:
        return rows[:SAMPLE_N], "dataset"
    return [], "none"


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


async def _upstream_dataset_rows(s, graph: Graph, node_id: str) -> list[dict]:
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
    out: list[dict] = []
    for ds_id in dataset_ids:
        recs = (await s.execute(select(DatasetRow).where(DatasetRow.dataset_id == ds_id)
                                .order_by(DatasetRow.idx).limit(SAMPLE_N))).scalars().all()
        out.extend(json.loads(r.data_json) for r in recs)
        if len(out) >= SAMPLE_N:
            break
    return out
