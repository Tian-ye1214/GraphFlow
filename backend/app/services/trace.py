import json
from copy import deepcopy

TRACE_ID_KEY = "_gf_trace_id"
PARENT_TRACE_ID_KEY = "_gf_parent_trace_id"
TRACE_KEYS = {TRACE_ID_KEY, PARENT_TRACE_ID_KEY}


def make_root_trace_id(run_id: int, node_id: str, row_no: int) -> str:
    return f"run{run_id}:{node_id}:{row_no}"


def make_child_trace_id(parent: str, node_id: str, row_idx: int, child_idx: int) -> str:
    return f"{parent}|{node_id}:{row_idx}:{child_idx}"


def row_trace_id(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get(TRACE_ID_KEY) or "")


def parent_trace_id(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get(PARENT_TRACE_ID_KEY) or "")


def strip_trace_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in TRACE_KEYS}


def strip_trace_rows(rows: list[dict]) -> list[dict]:
    return [strip_trace_row(r) if isinstance(r, dict) else r for r in rows]


def strip_trace_json(data_json: str) -> list[dict]:
    return strip_trace_rows(json.loads(data_json or "[]"))


def attach_root_trace(rows: list[dict], *, run_id: int, node_id: str, start: int = 0) -> list[dict]:
    """给根行打 trace。start：全局起始行号——无输入生成循环按批生成时传本节点已生成行的游标，
    使各批种子的 root trace 唯一不撞（否则每批都从 0 起会让不同批的同序行 trace 碰撞、诊断串行）。"""
    traced: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            traced.append(row)
            continue
        out = deepcopy(row)
        out.setdefault(TRACE_ID_KEY, make_root_trace_id(run_id, node_id, start + i))
        out.setdefault(PARENT_TRACE_ID_KEY, "")
        traced.append(out)
    return traced


def attach_child_trace(input_row: dict, out_rows: list[dict], *, node_id: str,
                       row_idx: int) -> list[dict]:
    parent = row_trace_id(input_row)
    traced: list[dict] = []
    for i, row in enumerate(out_rows):
        if not isinstance(row, dict):
            traced.append(row)
            continue
        out = deepcopy(row)
        if parent:
            if len(out_rows) == 1:
                out.setdefault(TRACE_ID_KEY, parent)
                out.setdefault(PARENT_TRACE_ID_KEY, parent_trace_id(input_row))
            else:
                out[TRACE_ID_KEY] = make_child_trace_id(parent, node_id, row_idx, i)
                out[PARENT_TRACE_ID_KEY] = parent
        else:
            out.setdefault(TRACE_ID_KEY, f"{node_id}:{row_idx}:{i}")
            out.setdefault(PARENT_TRACE_ID_KEY, "")
        traced.append(out)
    return traced


def rows_matching_trace(rows: list[dict], trace_id: str) -> list[dict]:
    return [r for r in rows if isinstance(r, dict) and row_trace_id(r) == trace_id]
