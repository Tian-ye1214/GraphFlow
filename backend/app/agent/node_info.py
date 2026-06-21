"""节点助手只读上下文工具：图拓扑 + 运行观测(本节点产出/失败行 + 运行汇总)。
全部按 (user_id, workflow_id) 租户校验、绑定当前节点；结果走 _fit_budget 防爆 wrap_tools 20k。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget, _safe_rows
from app.engine.graph import parse_graph
from app.models import ModelCallLog, QcFailure, Run, RunNodeState, RunRow, Workflow

OUTPUT_ROW_LIMIT = 20


def _clip(s: str | None, n: int = 400) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…[截断]"


def _summarize_node(node) -> dict:
    """节点关键配置摘要(供 agent 理解上游产出/下游所需；提示词截断防爆)。"""
    c = node.config or {}
    t = node.type
    if t == "input":
        out = {"dataset_ids": c.get("dataset_ids", [])}
    elif t == "llm_synth":
        out = {"model_config_id": c.get("model_config_id"),
               "output_mode": c.get("output_mode", "column"),
               "output_column": c.get("output_column"), "output_columns": c.get("output_columns"),
               "system_prompt": _clip(c.get("system_prompt")), "user_prompt": _clip(c.get("user_prompt"))}
    elif t == "qc":
        out = {"judge_model_ids": c.get("judge_model_ids")
               or ([c["model_config_id"]] if c.get("model_config_id") else []),
               "pass_k": c.get("pass_k", 1), "max_rounds": c.get("max_rounds"),
               "status_column": c.get("status_column", "qc_status"),
               "feedback_column": c.get("feedback_column", "qc_feedback"),
               "system_prompt": _clip(c.get("system_prompt")), "user_prompt": _clip(c.get("user_prompt"))}
    elif t == "auto_process":
        out = {"operations": [op.get("op") for op in c.get("operations", [])]}
    elif t == "http_fetch":
        out = {"method": c.get("method", "GET"), "url": c.get("url"), "extract": c.get("extract")}
    elif t == "output":
        out = {"save_as_dataset": c.get("save_as_dataset", False), "dataset_name": c.get("dataset_name")}
    else:
        out = {}
    return {k: v for k, v in out.items() if v not in (None, [], {})}


class NodeInfoTools:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 workflow_id: int, node_id: str):
        self._sf = session_factory
        self._uid = user_id
        self._wf_id = workflow_id
        self._node_id = node_id

    async def _owned_wf(self, session) -> Workflow | None:
        wf = await session.get(Workflow, self._wf_id)
        return wf if wf is not None and wf.user_id == self._uid else None

    async def _latest_run(self, session) -> Run | None:
        return (await session.execute(
            select(Run).where(Run.workflow_id == self._wf_id, Run.user_id == self._uid)
            .order_by(Run.id.desc()).limit(1))).scalars().first()

    async def show_workflow_graph(self) -> str:
        """看整条工作流图：所有节点(id/类型/关键配置摘要含提示词)、连线(普通/回扫)，并标出当前节点。
        配本节点前先看它——了解上游 LLM 产出什么列、下游(尤其质检 qc)按什么标准/引用什么列判定。"""
        async with self._sf() as s:
            wf = await self._owned_wf(s)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            try:
                graph = parse_graph(wf.graph_json)
            except Exception:
                return json.dumps({"error": "graph_unparseable"}, ensure_ascii=False)
            nodes = [{"id": n.id, "type": n.type, "config": _summarize_node(n)} for n in graph.nodes]
            edges = [{"source": e["source"], "target": e["target"], "kind": e["kind"]}
                     for e in graph.edges]
            return json.dumps(_fit_budget(
                {"workflow_name": wf.name, "current_node_id": self._node_id,
                 "rows": nodes, "edges": edges}, key="rows"), ensure_ascii=False)

    async def latest_run_summary(self) -> str:
        """看本工作流最近一次运行的状态/统计/逐节点(done·total·failed)。据失败情况调整本节点配置。"""
        async with self._sf() as s:
            wf = await self._owned_wf(s)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            run = await self._latest_run(s)
            if run is None:
                return json.dumps({"run_id": None, "rows": []}, ensure_ascii=False)
            states = (await s.execute(select(RunNodeState).where(
                RunNodeState.run_id == run.id))).scalars().all()
            rows = [{"node_id": st.node_id, "status": st.status, "total": st.total,
                     "done": st.done, "failed": st.failed} for st in states]
            return json.dumps({"run_id": run.id, "status": run.status,
                               "error": run.error, "stats": json.loads(run.stats_json),
                               "rows": rows}, ensure_ascii=False)

    async def read_node_output(self, status: str = "done", limit: int = 5) -> str:
        """看本节点在最近一次运行的产出行(status=done)或失败行(status=failed: 行号/错误/重试次数)。
        据上轮真实产出质量/失败原因迭代提示词。
        Parameters:
            status: done(产出行) 或 failed(失败行)
            limit: 最多返回条数，默认 5，上限 20
        """
        if status not in ("done", "failed"):
            return json.dumps({"error": "status 必须为 done 或 failed"}, ensure_ascii=False)
        limit = max(1, min(int(limit), OUTPUT_ROW_LIMIT))
        async with self._sf() as s:
            wf = await self._owned_wf(s)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            run = await self._latest_run(s)
            if run is None:
                return json.dumps({"run_id": None, "rows": []}, ensure_ascii=False)
            recs = (await s.execute(select(RunRow).where(
                RunRow.run_id == run.id, RunRow.node_id == self._node_id,
                RunRow.status == status).order_by(RunRow.row_idx))).scalars().all()
            if status == "failed":
                rows = [{"row_idx": r.row_idx, "attempt": r.attempt, "error": r.error}
                        for r in recs][:limit]
            else:
                rows = []
                for r in recs:
                    for row in json.loads(r.data_json):
                        if isinstance(row, dict):
                            rows.append(row)
                            if len(rows) >= limit:
                                break
                    if len(rows) >= limit:
                        break
                rows, _ = _safe_rows(rows, 500)
            return json.dumps(_fit_budget(
                {"run_id": run.id, "node_id": self._node_id, "status": status, "rows": rows}),
                ensure_ascii=False)

    async def read_qc_failures(self, limit: int = 10) -> str:
        """看最近一次运行的质检失败样本与各判定模型的不通过理由(据真实误判迭代质检提示词)。
        每条带其所属 qc 节点 id。
        Parameters:
            limit: 最多返回失败样本数，默认 10，上限 50
        """
        limit = max(1, min(int(limit), 50))
        async with self._sf() as s:
            wf = await self._owned_wf(s)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            run = await self._latest_run(s)
            if run is None:
                return json.dumps({"run_id": None, "rows": []}, ensure_ascii=False)
            recs = (await s.execute(select(QcFailure).where(QcFailure.run_id == run.id)
                                    .order_by(QcFailure.id).limit(limit))).scalars().all()
            rows = [{"node_id": f.node_id, "sample": json.loads(f.sample_json),
                     "reasons": json.loads(f.reasons_json)} for f in recs]
            return json.dumps(_fit_budget({"run_id": run.id, "rows": rows}), ensure_ascii=False)

    async def read_node_model_logs(self, limit: int = 5) -> str:
        """看本节点在最近一次运行的模型调用：渲染后的请求消息 + 模型回复正文 + tokens。
        定位"提示词渲染成什么/模型实际返回什么"的问题。
        Parameters:
            limit: 最多返回条数，默认 5，上限 20
        """
        limit = max(1, min(int(limit), 20))
        async with self._sf() as s:
            wf = await self._owned_wf(s)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            run = await self._latest_run(s)   # 按 run_id 限定(run 日志不带 workflow_id)，自然限本工作流+本租户
            if run is None:
                return json.dumps({"run_id": None, "rows": []}, ensure_ascii=False)
            recs = (await s.execute(select(ModelCallLog).where(
                ModelCallLog.run_id == run.id, ModelCallLog.node_id == self._node_id,
                ModelCallLog.user_id == self._uid).order_by(ModelCallLog.id.desc())
                .limit(limit))).scalars().all()
            rows = [{"model_name": r.model_name, "request": json.loads(r.request_json or "[]"),
                     "response": r.response_json, "prompt_tokens": r.prompt_tokens,
                     "completion_tokens": r.completion_tokens} for r in recs]
            return json.dumps(_fit_budget(
                {"run_id": run.id, "node_id": self._node_id, "rows": rows}), ensure_ascii=False)


def make_node_info_tools(session_factory: async_sessionmaker, user_id: int,
                         workflow_id: int, node_id: str) -> list:
    t = NodeInfoTools(session_factory, user_id, workflow_id, node_id)
    return [t.show_workflow_graph, t.latest_run_summary, t.read_node_output,
            t.read_qc_failures, t.read_node_model_logs]
