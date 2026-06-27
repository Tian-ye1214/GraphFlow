"""Agent run 控制工具：直连 DB + 归属校验的 pydantic-ai 工具，写入复用 run_service 单点。
范式同 GraphToolkit：读返回 JSON 串、错误返回人话串，绝不抛框架。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                        Workflow)


class RunToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned_run(self, s, run_id: int):
        run = await s.get(Run, int(run_id))
        return run if run is not None and run.user_id == self._uid else None

    async def list_runs(self, workflow_id: int | None = None) -> str:
        """列本租户运行(id/工作流名/状态/创建时间/QC首轮通过)。可按 workflow_id 筛。"""
        async with self._sf() as s:
            stmt = (select(Run, Workflow.name).join(Workflow, Run.workflow_id == Workflow.id)
                    .where(Run.user_id == self._uid).order_by(Run.id.desc()))
            if workflow_id is not None:
                stmt = stmt.where(Run.workflow_id == workflow_id)
            rows = (await s.execute(stmt)).all()
            return json.dumps(_fit_budget({"rows": [
                {"id": r.id, "workflow_id": r.workflow_id, "workflow_name": name,
                 "status": r.status, "error": r.error, "created_at": r.created_at.isoformat()}
                for r, name in rows]}), ensure_ascii=False)

    async def get_run(self, run_id: int) -> str:
        """看单次运行状态/统计/各节点进度(total/done/failed)/错误。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            states = (await s.execute(
                select(RunNodeState).where(RunNodeState.run_id == run.id))).scalars().all()
            return json.dumps(_fit_budget({
                "id": run.id, "status": run.status, "error": run.error,
                "stats": json.loads(run.stats_json),
                "node_states": [{"node_id": st.node_id, "status": st.status, "total": st.total,
                                 "done": st.done, "failed": st.failed} for st in states]},
                key="node_states"), ensure_ascii=False)

    async def read_run_rows(self, run_id: int, node_id: str, status: str | None = None,
                            limit: int = 20) -> str:
        """读运行某节点的输出/失败行。status 可选 done/failed 筛选。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            stmt = select(RunRow).where(RunRow.run_id == run.id, RunRow.node_id == node_id)
            if status is not None:
                stmt = stmt.where(RunRow.status == status)
            rows = (await s.execute(stmt.order_by(RunRow.row_idx)
                    .limit(min(max(int(limit), 1), 100)))).scalars().all()
            return json.dumps(_fit_budget({"rows": [
                {"row_idx": r.row_idx, "status": r.status, "data": json.loads(r.data_json)}
                for r in rows]}), ensure_ascii=False)

    async def read_run_logs(self, run_id: int, kind: str = "system",
                            node_id: str | None = None, limit: int = 100) -> str:
        """读运行日志：kind=system 系统日志 / kind=model 模型调用日志(可按 node_id 筛)。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            cap = min(max(int(limit), 1), 200)
            if kind == "model":
                stmt = select(ModelCallLog).where(ModelCallLog.run_id == run.id)
                if node_id is not None:
                    stmt = stmt.where(ModelCallLog.node_id == node_id)
                ms = (await s.execute(stmt.order_by(ModelCallLog.id.desc()).limit(cap))).scalars().all()
                data = [{"node_id": m.node_id, "source": m.source, "model_name": m.model_name,
                         "completion_tokens": m.completion_tokens} for m in ms]
            else:
                ls = (await s.execute(select(RunLog).where(RunLog.run_id == run.id)
                      .order_by(RunLog.id).limit(cap))).scalars().all()
                data = [{"node_id": l.node_id, "level": l.level, "message": l.message} for l in ls]
            return json.dumps(_fit_budget({"rows": data}), ensure_ascii=False)

    async def read_run_qc(self, run_id: int, node_id: str | None = None, limit: int = 20) -> str:
        """读运行质检：各 QC 节点指标(总数/首轮通过) + 失败样本(含各模型理由)。"""
        async with self._sf() as s:
            run = await self._owned_run(s, run_id)
            if run is None:
                return json.dumps({"error": "run_not_found"}, ensure_ascii=False)
            metrics = (await s.execute(
                select(QcMetric).where(QcMetric.run_id == run.id))).scalars().all()
            fstmt = select(QcFailure).where(QcFailure.run_id == run.id)
            if node_id is not None:
                fstmt = fstmt.where(QcFailure.node_id == node_id)
            fails = (await s.execute(fstmt.order_by(QcFailure.id)
                     .limit(min(max(int(limit), 1), 100)))).scalars().all()
            return json.dumps(_fit_budget({
                "metrics": [{"node_id": m.node_id, "total": m.total,
                             "first_round_pass": m.first_round_pass} for m in metrics],
                "failures": [{"node_id": f.node_id, "sample": json.loads(f.sample_json or "null"),
                              "reasons": json.loads(f.reasons_json or "[]")} for f in fails]},
                key="failures"), ensure_ascii=False)

    @property
    def tools(self) -> list:
        return [self.list_runs, self.get_run, self.read_run_rows,
                self.read_run_logs, self.read_run_qc]
