"""Agent run 控制工具：直连 DB + 归属校验的 pydantic-ai 工具，写入复用 run_service 单点。
范式同 GraphToolkit：读返回 JSON 串、错误返回人话串，绝不抛框架。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.config import settings
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.engine.manager import manager
from app.events import publish
from app.models import (ModelCallLog, QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                        User, Workflow, WorkflowVersion)
from app.routers.runs import _has_failed_rows, _prepare_rerun_failed, _rerun_scope
from app.services.run_service import (enqueue_run, purge_run_rows,
                                      restore_workflow_from_run as _restore_from_run,
                                      unlink_run_exports, validate_graph_resource_ownership)


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

    async def start_run(self, workflow_id: int) -> str:
        """启动一次运行(不阻塞，返回 run_id；用 get_run 看进度)。
        Parameters:
            workflow_id: 工作流 id
        """
        try:
            async with self._sf() as s:
                wf = await s.get(Workflow, int(workflow_id))
                if wf is None or wf.user_id != self._uid:
                    return "工作流不存在"
                try:
                    graph = parse_graph(wf.graph_json)
                    validate_graph(graph)
                    if not graph.nodes:
                        raise GraphError("工作流为空")
                    await validate_graph_resource_ownership(s, graph, self._uid)
                except (GraphError, ValueError) as e:
                    return f"Error: {e}"
            run_id = await enqueue_run(self._sf, self._uid, workflow_id)
            return f"已启动运行 #{run_id}（排队中），用 get_run({run_id}) 看进度"
        except Exception as e:
            return f"Error: {e}"

    async def cancel_run(self, run_id: int) -> str:
        """取消运行中/排队中的运行。"""
        try:
            async with self._sf() as s:
                run = await self._owned_run(s, run_id)
                if run is None:
                    return "运行不存在"
                if run.status not in ("queued", "running"):
                    return f"Error: 当前状态 {run.status} 不可取消"
            manager.cancel(int(run_id))
            return f"已请求取消运行 #{run_id}"
        except Exception as e:
            return f"Error: {e}"

    async def rerun_failed(self, run_id: int, node_id: str | None = None) -> str:
        """重跑失败行(可指定节点)。复用 manager 入队。"""
        try:
            async with self._sf() as s:
                run = await self._owned_run(s, run_id)
                if run is None:
                    return "运行不存在"
                cap = (await s.get(User, self._uid)).max_llm_concurrency
                ver = await s.get(WorkflowVersion, run.workflow_version_id)
                graph = parse_graph(ver.graph_json)
                if node_id is not None and node_id not in {n.id for n in graph.nodes}:
                    return f"Error: 节点 {node_id} 不在该运行的图中"
                scope = _rerun_scope(graph, node_id)
                if run.status in ("completed", "failed", "cancelled"):
                    if not await _has_failed_rows(s, run.id, scope):
                        return "Error: 没有失败行"
                    await _prepare_rerun_failed(self._sf, int(run_id), node_id, self._uid)
                    manager.submit(int(run_id), self._uid, cap, self._sf)
                    return f"已重跑运行 #{run_id} 的失败行"
                elif run.status not in ("queued", "running"):
                    return f"Error: 当前状态 {run.status} 不可重跑"
                # active run：等当前跑完再重跑
                async def prepare() -> bool:
                    return await _prepare_rerun_failed(self._sf, int(run_id), node_id, self._uid)
                manager.submit_prepared(int(run_id), self._uid, cap, self._sf, prepare)
                return f"已请求重跑运行 #{run_id} 的失败行（将在当前运行完成后执行）"
        except Exception as e:
            return f"Error: {e}"

    async def restore_workflow_from_run(self, run_id: int) -> str:
        """把工作流图恢复到该运行的版本(覆盖当前图)。需用户确认。
        Parameters:
            run_id: 运行 id
        """
        try:
            async with self._sf() as s:
                run = await self._owned_run(s, run_id)
                if run is None:
                    return "运行不存在"
                if not self._confirm_delete:
                    return ("恢复工作流版本会覆盖当前图(丢失当前未跑的编辑)，需用户确认："
                            f"请说明后在回复末尾单独一行输出 [confirm_delete] 恢复运行#{run_id}的版本，等待确认。")
                wf = await _restore_from_run(s, run, self._uid)
                return f"已把工作流恢复到运行 #{run_id} 的版本" if wf else "工作流不存在"
        except Exception as e:
            return f"Error: {e}"

    async def delete_run(self, run_id: int) -> str:
        """删除单次运行(级联子表+磁盘导出)。需用户确认。
        Parameters:
            run_id: 运行 id
        """
        try:
            async with self._sf() as s:
                run = await self._owned_run(s, run_id)
                if run is None:
                    return "运行不存在"
                if run.status in ("queued", "running"):
                    return "Error: 运行中，请先取消再删除"
                if not self._confirm_delete:
                    return ("删除运行需用户确认：请向用户说明将删除运行及其全部行/日志/质检/导出，"
                            f"在回复末尾单独一行输出 [confirm_delete] gf rmrun {run_id}，然后结束回合等待确认。")
                ver_id = run.workflow_version_id
                await purge_run_rows(s, [int(run_id)], version_ids=[ver_id])
                await s.commit()
            unlink_run_exports([int(run_id)], settings.data_dir)
            publish(self._uid, "run", int(run_id))
            return f"已删除运行 #{run_id}"
        except Exception as e:
            return f"Error: {e}"

    async def delete_all_runs(self) -> str:
        """清空本租户全部运行(运行中除外)。需用户确认。"""
        try:
            async with self._sf() as s:
                runs = (await s.execute(select(Run).where(
                    Run.user_id == self._uid, Run.status.notin_(("queued", "running"))))).scalars().all()
                if not self._confirm_delete:
                    return ("清空全部运行需用户确认：请向用户说明将删除全部已结束运行及其数据，"
                            "在回复末尾单独一行输出 [confirm_delete] 清空全部运行记录，然后结束回合等待确认。")
                run_ids = [r.id for r in runs]
                ver_ids = [r.workflow_version_id for r in runs]
                if run_ids:
                    await purge_run_rows(s, run_ids, version_ids=ver_ids)
                    await s.commit()
            if run_ids:
                unlink_run_exports(run_ids, settings.data_dir)
            return f"已删除 {len(run_ids)} 条运行记录"
        except Exception as e:
            return f"Error: {e}"

    @property
    def tools(self) -> list:
        return [self.list_runs, self.get_run, self.read_run_rows,
                self.read_run_logs, self.read_run_qc,
                self.start_run, self.cancel_run, self.rerun_failed,
                self.restore_workflow_from_run, self.delete_run, self.delete_all_runs]
