"""GraphFlow 图变更纯函数：对 graph dict（{"nodes":[...], "edges":[...]}）做结构/配置变更，
不碰 DB。失败抛 GraphOpError。gf CLI 与 Agent GraphToolkit 共用此单点（去重）。"""


class GraphOpError(ValueError):
    """图变更非法（节点不存在、类型未知、边重复、未知配置键等）。调用方 catch 后 die/返回错误串。
    是 ValueError 子类，故 gf CLI 顶层 except ValueError→die 会自动把它转成命令行错误。"""


NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "qc": "qc", "llm_synth": "llm_synth", "auto_process": "auto_process",
              "http": "http_fetch", "http_fetch": "http_fetch"}


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    raise GraphOpError(f"节点 {node_id} 不存在")


def add_node(graph: dict, node_type: str, node_id: str | None = None) -> str:
    ntype = NODE_TYPES.get(node_type)
    if ntype is None:
        raise GraphOpError(f"未知节点类型 {node_type}（可选: input/llm/auto/output/qc/http）")
    nodes = graph["nodes"]
    if node_id:
        if any(n["id"] == node_id for n in nodes):
            raise GraphOpError(f"节点 {node_id} 已存在")
    else:
        i = 1
        while any(n["id"] == f"{ntype}_{i}" for n in nodes):
            i += 1
        node_id = f"{ntype}_{i}"
    nodes.append({"id": node_id, "type": ntype,
                  "position": {"x": 80 + len(nodes) * 50, "y": 80 + len(nodes) * 40},
                  "config": {}})
    return node_id


def remove_node(graph: dict, node_id: str) -> None:
    find_node(graph, node_id)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]
    graph["edges"] = [e for e in graph["edges"] if node_id not in (e["source"], e["target"])]


def connect(graph: dict, source: str, target: str, kind: str) -> None:
    src = find_node(graph, source)
    find_node(graph, target)
    if kind == "rescan" and src["type"] != "qc":
        raise GraphOpError("rescan 回扫边必须从 qc 节点出发")
    if any(e["source"] == source and e["target"] == target for e in graph["edges"]):
        raise GraphOpError("连线已存在")
    graph["edges"].append({"source": source, "target": target, "kind": kind})


def disconnect(graph: dict, source: str, target: str) -> None:
    before = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"]
                      if not (e["source"] == source and e["target"] == target)]
    if len(graph["edges"]) == before:
        raise GraphOpError(f"不存在连线 {source} -> {target}")


LLM_CONFIG_KEYS = {"system": "system_prompt", "prompt": "user_prompt", "out": "output_column",
                   "mode": "output_mode", "fanout": "fanout_n", "conc": "concurrency",
                   "retries": "retries"}
LLM_PARAM_KEYS = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
                  "timeout": "timeout", "json_mode": "json_mode"}
INT_KEYS = {"fanout_n", "concurrency", "retries", "max_tokens", "timeout"}
FLOAT_KEYS = {"temperature", "top_p"}
HTTP_STR_KEYS = {"url", "endpoint", "method", "body"}
RESOLVE_KEYS = {"dataset": ("datasets", True), "model": ("models", False),
                "judge_models": ("models", True)}
OP_LABELS = {"dedup": "去重", "filter": "过滤", "rename": "重命名", "drop": "删除列",
             "concat": "拼接列", "cast": "类型转换", "sample": "随机采样", "shuffle": "打乱"}


def _convert(field: str, v):
    if field in INT_KEYS:
        return int(v)
    if field in FLOAT_KEYS:
        return float(v)
    if field == "json_mode":
        return str(v).lower() in ("true", "1", "yes")
    return v


def _parse_colon_map(v, key: str, fmt: str) -> dict:
    """解析 `a:b,c:d` 形式（首个冒号切分，值可含冒号）；已是 dict 直接返回。
    非空但缺冒号的段 → GraphOpError，不把用户输入静默吞成空 dict。"""
    if isinstance(v, dict):
        return v
    out = {}
    for seg in str(v).split(","):
        if not seg.strip():
            continue
        if ":" not in seg:
            raise GraphOpError(f"{key} 格式应为 {fmt}[,{fmt}]，缺少冒号: {seg!r}")
        k, val = seg.split(":", 1)
        out[k] = val
    return out


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x)]
    return [c for c in str(v).split(",") if c]


def apply_node_config(node: dict, key: str, value) -> None:
    """把一对 key/value 落到 node["config"]。resolve 键（dataset/model/judge_models）期望
    value 已是解析好的 id / id 列表（解析在调用方做，本函数不碰 DB）。未知键抛 GraphOpError。"""
    cfg = node["config"]
    if key == "dataset":
        cfg["dataset_ids"] = value if isinstance(value, list) else [value]
    elif key == "model":
        cfg["model_config_id"] = value
    elif key == "judge_models":
        cfg["judge_model_ids"] = value if isinstance(value, list) else [value]
    elif key == "save_as":
        cfg["save_as_dataset"] = bool(value)
        cfg["dataset_name"] = value
    elif key == "pass_k":
        cfg["pass_k"] = int(value)
    elif key == "max_rounds":
        cfg["max_rounds"] = int(value)
    elif key == "count":
        cfg["count"] = int(value) if value not in ("", None) else None
    elif key in HTTP_STR_KEYS:
        cfg[key] = value
    elif key == "extract":
        cfg["extract"] = _parse_colon_map(value, "extract", "列:JSON路径")
    elif key == "headers":
        cfg["headers"] = _parse_colon_map(value, "headers", "名:值")
    elif key in LLM_CONFIG_KEYS:
        cfg[LLM_CONFIG_KEYS[key]] = _convert(LLM_CONFIG_KEYS[key], value)
    elif key in LLM_PARAM_KEYS:
        cfg.setdefault("params", {})[LLM_PARAM_KEYS[key]] = _convert(LLM_PARAM_KEYS[key], value)
    elif key == "drop":
        cfg["drop_columns"] = _as_list(value)
    elif key == "outs":
        cfg["output_columns"] = _as_list(value)
    elif key == "status_col":
        cfg["status_column"] = value
    elif key == "feedback_col":
        cfg["feedback_column"] = value
    elif key == "think":
        cfg.setdefault("params", {})["thinking_enabled"] = str(value).lower() in ("on", "true", "1", "yes")
    elif key == "effort":
        cfg.setdefault("params", {})["reasoning_effort"] = value
    else:
        raise GraphOpError(f"未知配置键 {key}")


def build_op(op: str, params: list[str]) -> dict:
    if op == "dedup":
        return {"op": "dedup", "columns": params[0].split(",") if params else []}
    if op == "filter":
        if len(params) != 3:
            raise GraphOpError("filter 用法: <列> <min_len|max_len|contains|not_contains|regex> <值>")
        col, mode, value = params
        return {"op": "filter", "column": col, "mode": mode,
                "value": int(value) if mode in ("min_len", "max_len") else value}
    if op == "rename":
        if len(params) != 2:
            raise GraphOpError("rename 用法: <原列> <新列>")
        return {"op": "rename", "mapping": {params[0]: params[1]}}
    if op == "drop":
        if len(params) != 1:
            raise GraphOpError("drop 用法: <列1,列2>")
        return {"op": "drop", "columns": params[0].split(",")}
    if op == "concat":
        if len(params) < 2:
            raise GraphOpError("concat 用法: <列1,列2> <目标列> [分隔符]")
        return {"op": "concat", "columns": params[0].split(","), "target": params[1],
                "sep": params[2] if len(params) > 2 else ""}
    if op == "cast":
        if len(params) != 2 or params[1] not in ("str", "int", "float"):
            raise GraphOpError("cast 用法: <列> <str|int|float>")
        return {"op": "cast", "column": params[0], "to": params[1]}
    if op == "sample":
        if len(params) != 1:
            raise GraphOpError("sample 用法: <n>")
        return {"op": "sample", "n": int(params[0])}
    if op == "shuffle":
        return {"op": "shuffle"}
    raise GraphOpError(f"未知操作 {op}（可选: dedup/filter/rename/drop/concat/cast/sample/shuffle）")


def add_op(node: dict, op: str, params: list[str]) -> dict:
    if node["type"] != "auto_process":
        raise GraphOpError(f"{node['id']} 不是自动处理节点(auto_process)")
    built = build_op(op, params)
    node["config"].setdefault("operations", []).append(built)
    return built


def remove_op(node: dict, index: int) -> dict:
    if node["type"] != "auto_process":
        raise GraphOpError(f"{node['id']} 不是自动处理节点(auto_process)")
    ops = node["config"].setdefault("operations", [])
    if not 1 <= index <= len(ops):
        raise GraphOpError(f"序号超出范围（1-{len(ops)}）")
    return ops.pop(index - 1)
