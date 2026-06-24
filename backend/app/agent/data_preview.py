"""Read-only workflow data previews for agents and node assistants."""
import json
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.engine.columns import ordered_union
from app.engine.graph import parse_graph, upstream_ids
from app.models import Dataset, Run, RunRow, Workflow, WorkflowVersion
from app.services.dataset_store import MAX_AGENT_ROWS, read_dataset_range

PreviewSource = Literal["auto", "dataset", "latest_run"]

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
DEFAULT_CELL_CHAR_LIMIT = 500
MAX_PREVIEW_CHARS = 16000   # 预览 JSON 序列化预算；留余量给 wrap_tools 的 20000 上限，避免被腰斩成非法 JSON


def _coerce_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _safe_cell(value, char_limit: int) -> tuple[object, bool]:
    if value is None or isinstance(value, (int, float, bool)):
        return value, False
    if isinstance(value, str):
        text = value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= char_limit:
        return text, False
    return text[:char_limit] + "[truncated]", True


def _safe_rows(rows: list[dict], char_limit: int) -> tuple[list[dict], bool]:
    safe: list[dict] = []
    truncated = False
    for row in rows:
        out = {}
        for key, value in row.items():
            out[key], cell_truncated = _safe_cell(value, char_limit)
            truncated = truncated or cell_truncated
        safe.append(out)
    return safe, truncated


def _columns_from_rows(rows: list[dict]) -> list[str]:
    return ordered_union([list(r.keys()) for r in rows])


def _fit_budget(payload: dict, key: str = "rows") -> dict:
    """把 payload[key] 列表裁到序列化预算内，保证 json.dumps(payload) 完整可解析(不被 wrap_tools 20k 腰斩)。
    先按条裁(报 omitted_<key>)，单/少数超宽条仍超预算则逐级收紧单格上限再裁(cells_truncated_to)。
    若裁完 key 后整体仍超预算（兄弟 list 字段如 edges 体积大），逐步裁各兄弟 list 字段直至收敛。"""
    items = payload.get(key) or []
    kept, used = [], 0
    for r in items:
        size = len(json.dumps(r, ensure_ascii=False))
        if kept and used + size > MAX_PREVIEW_CHARS:
            break
        kept.append(r)
        used += size
    out = dict(payload)
    out[key] = kept
    omitted = len(items) - len(kept)
    limit = DEFAULT_CELL_CHAR_LIMIT
    while out[key] and limit > 50 and len(json.dumps(out, ensure_ascii=False)) > MAX_PREVIEW_CHARS:
        limit //= 2
        out[key], _ = _safe_rows(out[key], limit)
        out["cells_truncated_to"] = limit
    # Trim sibling list fields (e.g. edges) if the total payload is still oversized
    sibling_keys = [k for k, v in out.items() if k != key and isinstance(v, list)]
    for sib in sibling_keys:
        if len(json.dumps(out, ensure_ascii=False)) <= MAX_PREVIEW_CHARS:
            break
        sib_items = out[sib]
        # Binary-search trim: keep halving until within budget
        cap = len(sib_items)
        while cap > 0 and len(json.dumps(out, ensure_ascii=False)) > MAX_PREVIEW_CHARS:
            cap = cap // 2
            out[sib] = sib_items[:cap]
        omitted_sib = len(sib_items) - len(out[sib])
        if omitted_sib:
            out[f"omitted_{sib}"] = omitted_sib
            omitted = omitted or omitted_sib  # ensure hint is set
    if omitted:
        out[f"omitted_{key}"] = omitted
    if omitted or "cells_truncated_to" in out:
        out["hint"] = "结果按大小预算裁剪：可调小 limit / 缩小范围，或用 describe 看统计"
    return out


def _describe_column(name: str, rows: list[dict]) -> dict:
    """单列统计：dtype 分布 / 缺失率 / 低基数(≤15)值分布，否则报 distinct 估计。基于抽样行。"""
    from collections import Counter
    dt: Counter = Counter()
    missing = 0
    keys: list[str] = []
    for r in rows:
        v = r.get(name)
        if v is None:
            dt["null"] += 1
            missing += 1
        elif isinstance(v, bool):
            dt["bool"] += 1
        elif isinstance(v, int):
            dt["int"] += 1
        elif isinstance(v, float):
            dt["float"] += 1
        else:
            dt["str"] += 1
            if isinstance(v, str) and v.strip() == "":
                missing += 1
        keys.append(json.dumps(v, ensure_ascii=False, default=str))
    out = {"name": name, "dtypes": dict(dt),
           "missing_pct": round(missing / len(rows) * 100) if rows else 0}
    if len(set(keys)) <= 15:
        out["value_counts"] = dict(Counter(keys).most_common(15))
    else:
        out["distinct_estimate"] = len(set(keys))
    return out


class WorkflowDataPreview:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 cell_char_limit: int = DEFAULT_CELL_CHAR_LIMIT):
        self._session_factory = session_factory
        self._user_id = user_id
        self._cell_char_limit = cell_char_limit

    async def preview_workflow_data(self, workflow_id: int, node_id: str | None = None,
                                    source: PreviewSource = "auto", limit: int = DEFAULT_LIMIT) -> str:
        """Preview workflow data for the current user.
        Parameters:
            workflow_id: Workflow id to inspect
            node_id: Optional node id. For latest_run this returns that node's real input rows.
            source: auto, dataset, or latest_run. auto prefers latest_run and falls back to dataset.
            limit: Maximum rows to return, default 5, capped at 20
        """
        if source not in ("auto", "dataset", "latest_run"):
            return self._dump("none", [], [], error="invalid_source")
        row_limit = _coerce_limit(limit)
        async with self._session_factory() as session:
            wf = await session.get(Workflow, workflow_id)
            if wf is None or wf.user_id != self._user_id:
                return self._dump("none", [], [], error="workflow_not_found")
            if source in ("auto", "latest_run"):
                latest = await self._latest_run_preview(session, wf.id, node_id, row_limit)
                if latest["rows"]:
                    return json.dumps(_fit_budget(latest), ensure_ascii=False)
                if source == "latest_run":
                    return json.dumps(_fit_budget(latest), ensure_ascii=False)
            dataset = await self._dataset_preview(session, wf, row_limit)
            return json.dumps(_fit_budget(dataset), ensure_ascii=False)

    async def describe(self, workflow_id: int, node_id: str | None = None,
                       source: PreviewSource = "auto", sample_limit: int = MAX_LIMIT) -> str:
        """列 schema 概览：总行数(数据集源精确) + 每列 dtype 分布/缺失率/低基数值分布。列统计基于抽样行。"""
        if source not in ("auto", "dataset", "latest_run"):
            return self._dump("none", [], [], error="invalid_source")
        n = _coerce_limit(sample_limit)
        async with self._session_factory() as session:
            wf = await session.get(Workflow, workflow_id)
            if wf is None or wf.user_id != self._user_id:
                return self._dump("none", [], [], error="workflow_not_found")
            info, total = None, None
            if source in ("auto", "latest_run"):
                info = await self._latest_run_preview(session, wf.id, node_id, n)
                if not info["rows"] and source == "auto":
                    info = None
            if info is None:
                info = await self._dataset_preview(session, wf, n)
                total = await self._dataset_total_rows(session, wf)
            rows = info["rows"]
            cols = info["columns"] or _columns_from_rows(rows)
            payload = {"source": info["source"], "run_id": info.get("run_id"),
                       "total_rows": total, "sampled_rows": len(rows), "column_count": len(cols),
                       "columns": [_describe_column(c, rows) for c in cols]}
            return json.dumps(_fit_budget(payload, key="columns"), ensure_ascii=False)

    async def read_dataset_rows(
        self,
        dataset_id: int,
        start_row: int,
        end_row: int,
        columns: list[str] | None = None,
    ) -> str:
        """Read a visible file-row range from one dataset with optional column projection."""
        if not isinstance(start_row, int) or not isinstance(end_row, int):
            return self._dump("dataset_rows", [], [], error="invalid_row_range")
        if start_row < 1 or end_row < start_row:
            return self._dump("dataset_rows", [], [], error="invalid_row_range")
        if columns is not None and not (
            isinstance(columns, list) and all(isinstance(col, str) for col in columns)
        ):
            return self._dump("dataset_rows", [], [], error="invalid_columns")
        async with self._session_factory() as session:
            ds = await session.get(Dataset, dataset_id)
            if ds is None or ds.user_id != self._user_id:
                return json.dumps({"dataset_id": dataset_id, "rows": [], "error": "dataset_not_found"},
                                  ensure_ascii=False)
            payload = await read_dataset_range(
                session,
                ds,
                data_dir=settings.data_dir,
                start_row=start_row,
                end_row=end_row,
                columns=columns,
                max_rows=MAX_AGENT_ROWS,
                max_chars=MAX_PREVIEW_CHARS,
            )
            return json.dumps(_fit_budget(payload), ensure_ascii=False)

    async def _dataset_total_rows(self, session, wf: Workflow) -> int | None:
        """输入数据集总行数(精确，零扫行)；脏图无法解析则 None。"""
        try:
            graph = parse_graph(wf.graph_json)
        except Exception:
            return None
        total = 0
        for node in graph.nodes:
            if node.type != "input":
                continue
            for ds_id in node.config.get("dataset_ids", []):
                ds = await session.get(Dataset, ds_id)
                if ds is not None and ds.user_id == self._user_id:
                    total += ds.row_count
        return total

    def _dump(self, source: str, columns: list[str], rows: list[dict], *,
              run_id: int | None = None, truncated: bool = False, error: str | None = None) -> str:
        payload = {"source": source, "run_id": run_id, "columns": columns,
                   "rows": rows, "truncated": truncated}
        if error:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

    async def _dataset_preview(self, session, wf: Workflow, limit: int) -> dict:
        graph = parse_graph(wf.graph_json)
        dataset_ids = [
            ds_id
            for node in graph.nodes if node.type == "input"
            for ds_id in node.config.get("dataset_ids", [])
        ]
        columns_by_dataset: list[list[str]] = []
        rows: list[dict] = []
        for ds_id in dataset_ids:
            ds = await session.get(Dataset, ds_id)
            if ds is None or ds.user_id != self._user_id:
                continue
            columns_by_dataset.append(json.loads(ds.columns_json))
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            start = ds.data_start_row or 1
            page = await read_dataset_range(
                session, ds, data_dir=settings.data_dir,
                start_row=start, end_row=start + remaining - 1)
            rows.extend(row for row in page["rows"] if row.get("__row_type") != "header")
        safe_rows, truncated = _safe_rows(rows[:limit], self._cell_char_limit)
        columns = ordered_union(columns_by_dataset) or _columns_from_rows(safe_rows)
        return {"source": "dataset", "run_id": None, "columns": columns,
                "rows": safe_rows, "truncated": truncated}

    async def _latest_run_preview(self, session, workflow_id: int, node_id: str | None,
                                  limit: int) -> dict:
        runs = (await session.execute(
            select(Run).where(Run.workflow_id == workflow_id, Run.user_id == self._user_id)
            .order_by(Run.id.desc()).limit(20)
        )).scalars().all()
        for run in runs:
            rows = await self._run_rows_for_preview(session, run, node_id, limit)
            if rows:
                safe_rows, truncated = _safe_rows(rows[:limit], self._cell_char_limit)
                return {"source": "latest_run", "run_id": run.id,
                        "columns": _columns_from_rows(safe_rows),
                        "rows": safe_rows, "truncated": truncated}
        return {"source": "latest_run", "run_id": None, "columns": [],
                "rows": [], "truncated": False}

    async def _run_rows_for_preview(self, session, run: Run, node_id: str | None,
                                    limit: int) -> list[dict]:
        if not node_id:
            # row_count 报真实数据行数而非 RunRow 条数(否则 barrier 节点恒显 1)。为避免大 run 全量拉 data_json：
            # 逐行节点(llm_synth/http_fetch)用 RunRow 条数(≈输入行数；fanout>1 时略低于产出，概览足够)，
            # 只对 barrier 节点(各 1 条 RunRow 装全部行)读那条 data_json 算真实长度——IO 从 O(总行) 降到 O(barrier 节点)。
            ver = await session.get(WorkflowVersion, run.workflow_version_id)
            types: dict[str, str] = {}
            if ver is not None:
                try:
                    types = {n.id: n.type for n in parse_graph(ver.graph_json).nodes}
                except Exception:
                    types = {}
            grouped = (await session.execute(
                select(RunRow.node_id, func.count())
                .where(RunRow.run_id == run.id, RunRow.status == "done")
                .group_by(RunRow.node_id))).all()
            counts: dict[str, int] = {}
            barrier: list[str] = []
            for nid, cnt in grouped:
                if types.get(nid) in ("llm_synth", "http_fetch"):
                    counts[nid] = cnt
                else:
                    barrier.append(nid)
            if barrier:
                recs = (await session.execute(
                    select(RunRow.node_id, RunRow.data_json)
                    .where(RunRow.run_id == run.id, RunRow.status == "done",
                           RunRow.node_id.in_(barrier)))).all()
                for nid, data_json in recs:
                    rows = json.loads(data_json)
                    counts[nid] = counts.get(nid, 0) + (len(rows) if isinstance(rows, list) else 0)
            return [{"node_id": nid, "row_count": counts[nid]} for nid in sorted(counts)][:limit]

        ver = await session.get(WorkflowVersion, run.workflow_version_id)
        graph = parse_graph(ver.graph_json) if ver is not None else None
        if graph is None or node_id not in {n.id for n in graph.nodes}:
            return []
        parents = upstream_ids(graph, node_id)
        if not parents:
            return await self._flatten_run_rows(session, run.id, node_id, limit)
        branches = [await self._flatten_run_rows(session, run.id, parent, limit) for parent in parents]
        if len(branches) == 1:
            return branches[0]
        counts = [len(branch) for branch in branches]
        if not counts or len(set(counts)) != 1:
            return []
        merged: list[dict] = []
        for i in range(min(counts[0], limit)):
            row: dict = {}
            for branch in branches:
                row.update(branch[i])
            merged.append(row)
        return merged

    async def _flatten_run_rows(self, session, run_id: int, node_id: str,
                                limit: int) -> list[dict]:
        recs = (await session.execute(
            select(RunRow).where(RunRow.run_id == run_id, RunRow.node_id == node_id,
                                 RunRow.status == "done")
            .order_by(RunRow.row_idx)
        )).scalars().all()
        rows: list[dict] = []
        for rec in recs:
            for row in json.loads(rec.data_json):
                if isinstance(row, dict):
                    rows.append(row)
                    if len(rows) >= limit:
                        return rows
        return rows


def make_preview_tools(session_factory: async_sessionmaker, user_id: int,
                       workflow_id: int | None = None, node_id: str | None = None) -> list:
    previewer = WorkflowDataPreview(session_factory, user_id)
    if workflow_id is None:
        return [previewer.preview_workflow_data, previewer.read_dataset_rows]

    async def preview_current_node_input(source: str = "auto", limit: int = DEFAULT_LIMIT) -> str:
        """预览当前正在配置节点的输入数据，默认返回列名和前 5 行。
        Parameters:
            source: auto / dataset / latest_run；auto 优先最近运行产出，否则回退输入数据集
            limit: 最大样例行数，默认 5，系统上限 20
        """
        return await previewer.preview_workflow_data(
            workflow_id, node_id=node_id, source=source, limit=limit)

    async def describe_current_node_input(source: str = "auto", sample_limit: int = MAX_LIMIT) -> str:
        """统计当前节点输入数据：总行数(数据集源)、各列类型分布/缺失率、低基数列的值分布。
        配 cast/dedup/filter/质检阈值前先看它，判断列类型、是否大量缺失、是固定枚举还是自由文本。
        Parameters:
            source: auto / dataset / latest_run
            sample_limit: 统计抽样行数，默认 20，系统上限 20
        """
        return await previewer.describe(
            workflow_id, node_id=node_id, source=source, sample_limit=sample_limit)

    return [preview_current_node_input, describe_current_node_input, previewer.read_dataset_rows]
