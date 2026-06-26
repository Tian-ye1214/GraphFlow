"""Agent 图操作工具：把 workflow/node/edge 操作做成直连 DB 的 pydantic-ai 工具。
范式同 catalog/node_info（session_factory + user_id + 归属校验）；图变更走 graph_ops 单点，
落库+SSE 走 workflow_store。归属不符返回人话错误串，不抛异常到框架。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.agent.node_info import _summarize_node
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.events import publish
from app.models import Workflow
from app.services import graph_ops as go
from app.services.workflow_store import resolve_ref, update_workflow_graph


class GraphToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int):
        self._sf = session_factory
        self._uid = user_id

    async def _owned(self, session, workflow_id: int):
        wf = await session.get(Workflow, int(workflow_id))
        return wf if wf is not None and wf.user_id == self._uid else None

    async def _mutate(self, workflow_id: int, fn) -> str:
        """取属主工作流→对 graph dict 跑 fn(graph, session)（可 await）→落库+SSE。
        fn 返回成功消息串；GraphOpError→错误串；非属主→「工作流不存在」。"""
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            graph = json.loads(wf.graph_json)
            try:
                msg = await fn(graph, s)
            except go.GraphOpError as e:
                return f"Error: {e}"
            await update_workflow_graph(s, wf, graph)
            return msg

    async def create_workflow(self, name: str) -> str:
        """新建一个空工作流，返回其 id。
        Parameters:
            name: 工作流名称
        """
        async with self._sf() as s:
            wf = Workflow(user_id=self._uid, name=name,
                          graph_json=json.dumps({"nodes": [], "edges": []}, ensure_ascii=False))
            s.add(wf)
            await s.commit()
            publish(self._uid, "workflow", wf.id)
            return f"已创建工作流「{name}」(#{wf.id})"

    async def rename_workflow(self, workflow_id: int, name: str) -> str:
        """重命名工作流。
        Parameters:
            workflow_id: 工作流 id
            name: 新名称
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            wf.name = name
            await s.commit()
            publish(self._uid, "workflow", wf.id)
            return f"已重命名工作流 #{workflow_id} -> {name}"

    async def delete_workflow(self, workflow_id: int) -> str:
        """删除工作流（连带其运行记录由既有级联保证）。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return "工作流不存在"
            await s.delete(wf)
            await s.commit()
            publish(self._uid, "workflow", workflow_id)
            return f"已删除工作流 #{workflow_id}"

    async def add_node(self, workflow_id: int, node_type: str, node_id: str | None = None) -> str:
        """给工作流加一个节点，返回其 id。
        Parameters:
            workflow_id: 工作流 id
            node_type: input/llm/auto/output/qc/http 之一
            node_id: 可选指定 id；留空自动生成
        """
        async def fn(graph, _s):
            nid = go.add_node(graph, node_type, node_id)
            return f"已添加节点 {nid}"
        return await self._mutate(workflow_id, fn)

    async def remove_node(self, workflow_id: int, node_id: str) -> str:
        """删除节点及其连线。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
        """
        async def fn(graph, _s):
            go.remove_node(graph, node_id)
            return f"已删除节点 {node_id} 及其连线"
        return await self._mutate(workflow_id, fn)

    async def connect_nodes(self, workflow_id: int, source: str, target: str,
                            kind: str = "normal") -> str:
        """连一条边。kind=normal 普通边；kind=rescan 质检回扫边(必须从 qc 节点出发)。
        Parameters:
            workflow_id: 工作流 id
            source: 源节点 id
            target: 目标节点 id
            kind: normal 或 rescan
        """
        async def fn(graph, _s):
            go.connect(graph, source, target, kind)
            return f"已连线 {source} -> {target}（{kind}）"
        return await self._mutate(workflow_id, fn)

    async def disconnect_nodes(self, workflow_id: int, source: str, target: str) -> str:
        """断开一条边。
        Parameters:
            workflow_id: 工作流 id
            source: 源节点 id
            target: 目标节点 id
        """
        async def fn(graph, _s):
            go.disconnect(graph, source, target)
            return f"已断开 {source} -> {target}"
        return await self._mutate(workflow_id, fn)

    async def set_node_config(self, workflow_id: int, node_id: str, config: dict) -> str:
        """设置节点配置（一次可设多个键）。键同 gf node set：
        model/dataset/judge_models(填名或id，自动解析)、prompt/system(提示词)、out/outs/mode/fanout/
        conc/retries、temp/top_p/max_tokens/timeout/json_mode(采样)、think/effort(思考)、
        url/endpoint/method/body/extract/headers(http)、pass_k/max_rounds/status_col/feedback_col(质检)、
        count(产量上限)、drop(删列)、save_as(存为数据集)。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
            config: {键: 值} 字典
        """
        async def fn(graph, s):
            node = go.find_node(graph, node_id)
            for key, raw in config.items():
                if key in go.RESOLVE_KEYS:
                    kind, is_list = go.RESOLVE_KEYS[key]
                    refs = (raw if isinstance(raw, list) else
                            [r for r in str(raw).split(",") if r]) if is_list else [raw]
                    ids = [await resolve_ref(s, self._uid, kind, r) for r in refs]
                    go.apply_node_config(node, key, ids if is_list else ids[0])
                else:
                    go.apply_node_config(node, key, raw)
            return f"已更新节点 {node_id} 配置"
        return await self._mutate(workflow_id, fn)

    async def set_node_prompt(self, workflow_id: int, node_id: str, slot: str,
                             body: str | None = None, library_ref: int | str | None = None,
                             mode: str = "copy") -> str:
        """设置节点的系统/用户提示词。直接传 body 写内联；或传 library_ref 用库提示词
        (mode=ref 运行时取最新版；mode=copy 复制当前正文进来)。
        Parameters:
            workflow_id: 工作流 id
            node_id: 节点 id
            slot: system 或 user
            body: 内联提示词正文（与 library_ref 二选一）
            library_ref: 库提示词 id 或名
            mode: ref(引用) 或 copy(复制，默认)
        """
        if slot not in ("system", "user"):
            return "Error: slot 必须为 system 或 user"
        field = "system_prompt" if slot == "system" else "user_prompt"

        async def fn(graph, s):
            node = go.find_node(graph, node_id)
            cfg = node["config"]
            if library_ref is not None:
                pid = await resolve_ref(s, self._uid, "prompts", library_ref)
                if mode == "ref":
                    cfg[f"{field}_ref"] = pid
                    return f"已将 {node_id} 的 {slot} 提示词设为引用库 #{pid}"
                from app.models import PromptVersion
                ver = (await s.execute(select(PromptVersion)
                       .where(PromptVersion.prompt_id == pid)
                       .order_by(PromptVersion.version.desc()).limit(1))).scalars().first()
                cfg[field] = ver.body if ver else ""
                cfg.pop(f"{field}_ref", None)
                return f"已复制库提示词 #{pid} 到 {node_id} 的 {slot}"
            if body is None:
                raise go.GraphOpError("需提供 body 或 library_ref")
            cfg[field] = body
            cfg.pop(f"{field}_ref", None)
            return f"已写入 {node_id} 的 {slot} 提示词（{len(body)} 字符）"
        return await self._mutate(workflow_id, fn)

    async def add_node_op(self, workflow_id: int, node_id: str, op: str, params: list[str]) -> str:
        """给自动处理节点追加一个操作。op ∈ dedup/filter/rename/drop/concat/cast/sample/shuffle。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
            op: 操作名
            params: 操作参数列表（如 dedup 用 ["列1,列2"]）
        """
        async def fn(graph, _s):
            built = go.add_op(go.find_node(graph, node_id), op, params)
            return f"已添加操作: {json.dumps(built, ensure_ascii=False)}"
        return await self._mutate(workflow_id, fn)

    async def remove_node_op(self, workflow_id: int, node_id: str, index: int) -> str:
        """删除自动处理节点的第 index 个操作（1-based）。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
            index: 操作序号，从 1 开始
        """
        async def fn(graph, _s):
            removed = go.remove_op(go.find_node(graph, node_id), int(index))
            return f"已删除操作: {go.OP_LABELS.get(removed['op'], removed['op'])}"
        return await self._mutate(workflow_id, fn)

    async def list_workflows(self) -> str:
        """列本租户全部工作流(id/名)。先用它拿到 workflow_id 再做后续操作。"""
        async with self._sf() as s:
            recs = (await s.execute(select(Workflow).where(Workflow.user_id == self._uid)
                                    .order_by(Workflow.id.desc()))).scalars().all()
            return json.dumps(_fit_budget(
                {"rows": [{"id": w.id, "name": w.name} for w in recs]}), ensure_ascii=False)

    async def show_workflow_graph(self, workflow_id: int) -> str:
        """看某工作流的图：所有节点(id/类型/关键配置摘要含提示词)与连线(普通/回扫)。
        Parameters:
            workflow_id: 工作流 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
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
                {"workflow_name": wf.name, "rows": nodes, "edges": edges}, key="rows"),
                ensure_ascii=False)

    async def workflow_columns(self, workflow_id: int, node_id: str | None = None) -> str:
        """看工作流列血缘：各节点的输入列与输出列（据此知道某节点能引用哪些 {{列}}、产出什么列）。
        草稿态/脏图无法计算列血缘时返回 graph_invalid。
        Parameters:
            workflow_id: 工作流 id
            node_id: 可选，仅看某个节点
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            try:
                graph = parse_graph(wf.graph_json)
                validate_graph(graph)
                dataset_cols = await resolve_dataset_cols(s, graph, self._uid)
                cols = propagate_columns(graph, dataset_cols)
            except (GraphError, AttributeError, TypeError, KeyError) as e:
                return json.dumps({"error": "graph_invalid", "detail": str(e)}, ensure_ascii=False)
            if node_id is not None:
                if node_id not in cols:
                    return json.dumps({"error": "node_not_found"}, ensure_ascii=False)
                cols = {node_id: cols[node_id]}
            rows = [{"node_id": nid, "input": io["input"], "output": io["output"]}
                    for nid, io in cols.items()]
            return json.dumps(_fit_budget({"rows": rows}), ensure_ascii=False)

    async def list_node_ops(self, workflow_id: int, node_id: str) -> str:
        """列自动处理节点的操作序列。
        Parameters:
            workflow_id: 工作流 id
            node_id: auto_process 节点 id
        """
        async with self._sf() as s:
            wf = await self._owned(s, workflow_id)
            if wf is None:
                return json.dumps({"error": "workflow_not_found"}, ensure_ascii=False)
            graph = json.loads(wf.graph_json)
            try:
                node = go.find_node(graph, node_id)
            except go.GraphOpError:
                return json.dumps({"error": "node_not_found"}, ensure_ascii=False)
            ops = node.get("config", {}).get("operations", [])
            return json.dumps({"rows": ops}, ensure_ascii=False)

    @property
    def tools(self) -> list:
        return [self.list_workflows, self.show_workflow_graph, self.workflow_columns,
                self.list_node_ops,
                self.create_workflow, self.rename_workflow, self.delete_workflow,
                self.add_node, self.remove_node, self.connect_nodes, self.disconnect_nodes,
                self.set_node_config, self.set_node_prompt, self.add_node_op, self.remove_node_op]
