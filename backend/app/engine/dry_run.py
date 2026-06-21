"""单节点试跑（dry_run）：用节点真实输入的小样本跑该节点逻辑，看渲染后的提示词 / 模型产出 / 产出列。

铁律——零副作用：绝不落 RunRow / 数据集 / 模型日志（suppress_model_log 清空日志上下文）、不改任何状态。
复用引擎纯函数（run_llm_synth_row / run_qc_judge_row / run_http_fetch_row / apply_operations_with_agent），
与正式 run 同源，所见即所跑。归属校验、config 形状校验、提示词引用解析均复用正式 run 的单点逻辑，防漂移。

样本上限：调模型节点(llm/qc/http) N≤3 控成本；本地变换 auto_process N≤20。fanout 恒为 1。
http_fetch 默认仅放行 GET/HEAD；非读方法需显式 allow_side_effects（避免一次"试跑"触发写/改远端）。
密钥铁律：产出绝不含 api_key / headers（headers 可能含 Authorization）。
"""
import asyncio

from app.agent.data_preview import WorkflowDataPreview, _columns_from_rows, _safe_rows
from app.engine.graph import Graph, Node, parse_graph
from app.engine.nodes import (TEMPLATE_RE, add_usage, apply_operations_with_agent, render_template,
                              run_http_fetch_row, run_llm_synth_row, run_qc_judge_row,
                              strip_qc_internal, zero_usage)
from app.engine.runner import _resolve_prompt_refs, validate_node_config_shape
from app.models import ModelConfig, User, Workflow
from app.services.model_log import suppress_model_log
from app.services.run_service import validate_graph_resource_ownership

MODEL_SAMPLE_CAP = 3          # 调模型/调接口节点：试跑样本上限（控成本/控副作用）
LOCAL_SAMPLE_CAP = 20         # auto_process 本地变换：样本上限
SAFE_HTTP_METHODS = ("GET", "HEAD")
SUPPORTED_TYPES = ("llm_synth", "qc", "http_fetch", "auto_process")


class DryRunNotFound(Exception):
    """工作流不存在/非本人，或节点 id 不在图中（端点映射 404）。"""


def _missing_cols(texts: list[str], row_keys) -> list[str]:
    keys = set(row_keys)
    want: set[str] = set()
    for t in texts:
        want |= set(TEMPLATE_RE.findall(t or ""))
    return sorted(want - keys)


async def dry_run_node(session_factory, user_id: int, workflow_id: int, node_id: str,
                       override_config: dict | None = None, limit: int = MODEL_SAMPLE_CAP,
                       call_model: bool = True, allow_side_effects: bool = False) -> dict:
    """试跑某节点。override_config 覆盖该节点当前 config（前端未保存的草稿）。
    call_model=False 仅渲染提示词不调模型（免费预览）。返回结构见模块顶部说明。
    raises: DryRunNotFound(404 类) / ValueError(422 类：归属、脏 config、不支持类型、引用缺失)。"""
    async with session_factory() as s:
        wf = await s.get(Workflow, workflow_id)
        if wf is None or wf.user_id != user_id:
            raise DryRunNotFound("工作流不存在")
        graph = parse_graph(wf.graph_json)
        user = await s.get(User, user_id)
    src_node = next((n for n in graph.nodes if n.id == node_id), None)
    if src_node is None:
        raise DryRunNotFound("节点不存在")
    cfg = {**src_node.config, **(override_config or {})}
    node = Node(id=node_id, type=src_node.type, config=cfg)
    if node.type not in SUPPORTED_TYPES:
        raise ValueError(f"节点类型 {node.type} 暂不支持试跑（仅支持 {', '.join(SUPPORTED_TYPES)}）")

    # 归属校验（仅本节点的资源，不波及图中其它未配好的节点）+ config 形状校验 + 引用解析，全复用正式 run 单点
    one = Graph(nodes=[node], edges=[])
    async with session_factory() as s:
        await validate_graph_resource_ownership(s, one, user_id)
    validate_node_config_shape(node)
    await _resolve_prompt_refs(session_factory, one, user_id)   # *_ref → 库提示词最新版正文，缺失 raise

    cap = MODEL_SAMPLE_CAP if node.type != "auto_process" else LOCAL_SAMPLE_CAP
    eff_limit = max(1, min(int(limit), cap))
    previewer = WorkflowDataPreview(session_factory, user_id)
    sample, sample_source, run_id = await previewer.node_input_sample(
        workflow_id, node_id, limit=eff_limit)

    out: dict = {"node_id": node_id, "node_type": node.type, "sample_source": sample_source,
                 "run_id": run_id, "sampled": len(sample), "limit": eff_limit,
                 "input_columns": _columns_from_rows(sample), "usage": zero_usage()}
    if not sample:
        out["note"] = "无可用输入样本：请先为输入节点选择数据集，或先运行一次以产生上游数据"
        out["rows"] = []
        return out

    # 试跑专用信号量（不复用 run manager 的全局缓存：跨测试 event loop 会绑错；样本 ≤3 顺序跑，
    # 多出的并发可忽略）。仍尊重用户配置的并发上限。
    sem = asyncio.Semaphore(user.max_llm_concurrency if user else 4)
    if node.type == "auto_process":
        await _dry_auto_process(out, node, sample)
    elif node.type == "llm_synth":
        await _dry_llm(out, node, sample, session_factory, user_id, sem, call_model)
    elif node.type == "qc":
        await _dry_qc(out, node, sample, session_factory, user_id, sem, call_model)
    elif node.type == "http_fetch":
        await _dry_http(out, node, sample, call_model, allow_side_effects)
    return out


async def _fetch_mc(session_factory, mc_id: int) -> ModelConfig:
    async with session_factory() as s:
        return await s.get(ModelConfig, mc_id)


async def _dry_llm(out, node, sample, session_factory, user_id, sem, call_model):
    cfg = node.config
    mc = await _fetch_mc(session_factory, cfg.get("model_config_id"))
    forced = {**cfg, "fanout_n": 1}   # 试跑恒单条，不套用 fanout（控成本）
    sys_t, usr_t = cfg.get("system_prompt", ""), cfg.get("user_prompt", "")
    rows = []
    with suppress_model_log():
        for row in sample:
            base = strip_qc_internal(row)
            entry = {"input": base, "rendered_system": render_template(sys_t, base),
                     "rendered_user": render_template(usr_t, base),
                     "missing_cols": _missing_cols([sys_t, usr_t], base)}
            if call_model:
                try:
                    out_rows, u = await run_llm_synth_row(forced, row, mc, sem)
                    add_usage(out["usage"], u)
                    produced = out_rows[0] if out_rows else {}
                    entry["output"] = produced
                    entry["new_columns"] = sorted(set(produced) - set(base))
                except Exception as e:
                    entry["error"] = str(e)
            rows.append(entry)
    out["rows"] = rows


async def _dry_qc(out, node, sample, session_factory, user_id, sem, call_model):
    cfg = node.config
    judge_ids = cfg.get("judge_model_ids") or (
        [cfg["model_config_id"]] if cfg.get("model_config_id") else [])
    async with session_factory() as s:
        jmcs = [await s.get(ModelConfig, jid) for jid in judge_ids]
    pass_k = cfg.get("pass_k", 1)
    try:
        pass_k = int(pass_k)
    except (TypeError, ValueError):
        pass_k = 1
    pass_k = max(1, min(pass_k, len(jmcs)))
    out["pass_k"] = pass_k
    sys_t, usr_t = cfg.get("system_prompt", ""), cfg.get("user_prompt", "")
    rows = []
    with suppress_model_log():
        for row in sample:
            base = strip_qc_internal(row)
            entry = {"input": base, "rendered_system": render_template(sys_t, base),
                     "rendered_user": render_template(usr_t, base),
                     "missing_cols": _missing_cols([sys_t, usr_t], base)}
            if call_model:
                passed, reason, u, per_model = await run_qc_judge_row(cfg, row, jmcs, pass_k, sem)
                add_usage(out["usage"], u)
                entry.update(passed=passed, reason=reason, per_model=per_model)
            rows.append(entry)
    out["rows"] = rows


async def _dry_http(out, node, sample, call_model, allow_side_effects):
    cfg = node.config
    method = (cfg.get("method") or "GET").upper()
    if method not in SAFE_HTTP_METHODS and not allow_side_effects:
        out["rows"] = []
        out["needs_confirm"] = True
        out["side_effect_note"] = (f"该节点使用 {method} 方法，可能写/改远端（有副作用）。"
                                   "试跑默认仅放行 GET/HEAD；如确认安全请允许副作用后再试。")
        return
    url_t = cfg.get("url", "")
    rows = []
    for row in sample:
        base = strip_qc_internal(row)
        # 渲染后的 url/method 入产出；headers 绝不入（可能含 Authorization）
        entry = {"input": base, "rendered_url": render_template(url_t, base), "method": method,
                 "missing_cols": _missing_cols(
                     [url_t, cfg.get("body") or "",
                      *[str(v) for v in (cfg.get("headers") or {}).values()]], base)}
        if call_model:
            try:
                out_rows, _u = await run_http_fetch_row(cfg, row)
                produced = out_rows[0] if out_rows else {}
                entry["output"] = produced
                entry["new_columns"] = sorted(set(produced) - set(base))
            except Exception as e:
                entry["error"] = str(e)
        rows.append(entry)
    out["rows"] = rows


async def _dry_auto_process(out, node, sample):
    cfg = node.config
    in_rows, _ = _safe_rows(sample, 500)
    out["input_rows"] = in_rows
    try:
        produced = await apply_operations_with_agent(sample, cfg.get("operations", []),
                                                     seed=cfg.get("seed"))
    except Exception as e:
        out["error"] = str(e)
        out["output_rows"] = []
        return
    safe_out, _ = _safe_rows(produced, 500)
    out["output_rows"] = safe_out
    out["output_columns"] = _columns_from_rows(produced)
