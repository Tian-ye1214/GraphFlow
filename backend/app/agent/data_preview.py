"""Read-only workflow data previews for agents and node assistants."""
import json
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine.graph import parse_graph, upstream_ids
from app.models import Dataset, DatasetRow, Run, RunRow, Workflow, WorkflowVersion

PreviewSource = Literal["auto", "dataset", "latest_run"]

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
DEFAULT_CELL_CHAR_LIMIT = 500


def _ordered_union(lists: list[list[str]]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for col in lst:
            if col not in out:
                out.append(col)
    return out


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
    return _ordered_union([list(r.keys()) for r in rows])


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
                    return json.dumps(latest, ensure_ascii=False)
                if source == "latest_run":
                    return json.dumps(latest, ensure_ascii=False)
            dataset = await self._dataset_preview(session, wf, row_limit)
            return json.dumps(dataset, ensure_ascii=False)

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
            recs = (await session.execute(
                select(DatasetRow).where(DatasetRow.dataset_id == ds.id)
                .order_by(DatasetRow.idx).limit(remaining)
            )).scalars().all()
            rows.extend(json.loads(r.data_json) for r in recs)
        safe_rows, truncated = _safe_rows(rows[:limit], self._cell_char_limit)
        columns = _ordered_union(columns_by_dataset) or _columns_from_rows(safe_rows)
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
            grouped = (await session.execute(
                select(RunRow.node_id, func.count())
                .where(RunRow.run_id == run.id, RunRow.status == "done")
                .group_by(RunRow.node_id)
                .order_by(RunRow.node_id)
            )).all()
            return [{"node_id": nid, "row_count": count} for nid, count in grouped][:limit]

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
        return [previewer.preview_workflow_data]

    async def preview_current_node_input(source: str = "auto", limit: int = DEFAULT_LIMIT) -> str:
        """预览当前正在配置节点的输入数据，默认返回列名和前 5 行。
        Parameters:
            source: auto / dataset / latest_run；auto 优先最近运行产出，否则回退输入数据集
            limit: 最大样例行数，默认 5，系统上限 20
        """
        return await previewer.preview_workflow_data(
            workflow_id, node_id=node_id, source=source, limit=limit)

    return [preview_current_node_input]
