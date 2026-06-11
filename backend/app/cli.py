"""gf —— GraphFlow 命令行客户端。所有操作通过 HTTP API 完成，与前端同权限。"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

STATE_FILE = Path.home() / ".graphflow" / "cli.json"
NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "llm_synth": "llm_synth", "auto_process": "auto_process"}
NODE_LABELS = {"input": "输入", "llm_synth": "LLM 合成", "auto_process": "自动处理", "output": "输出"}
KIND_LABELS = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置"}
STATUS_LABELS = {"queued": "排队中", "running": "运行中", "completed": "已完成",
                 "failed": "失败", "cancelled": "已取消", "pending": "等待", "done": "完成"}


def die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


class Cli:
    def __init__(self):
        self.state = load_state()
        if not self.state.get("cookie"):
            die("未登录，先执行: gf login <用户名>")
        self.http = httpx.Client(base_url=self.state["server"],
                                 cookies={"gf_session": self.state["cookie"]}, timeout=30)

    def check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            die(str(detail))
        return r

    def req(self, method: str, path: str, **kw):
        return self.check(self.http.request(method, path, **kw)).json()

    def resolve(self, kind: str, ref: str) -> int:
        """纯数字按 ID，否则按名字精确匹配。kind: workflows/datasets/models。"""
        if ref.isdigit():
            return int(ref)
        hits = [i for i in self.req("GET", f"/api/{kind}") if i["name"] == ref]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            die(f"找不到名为「{ref}」的{KIND_LABELS[kind]}")
        die(f"「{ref}」有 {len(hits)} 个同名项，请改用 ID: {[h['id'] for h in hits]}")

    def current_wf(self) -> int:
        wf_id = self.state.get("workflow_id")
        if not wf_id:
            die("未选择工作流，先执行: gf use <名|ID>")
        return wf_id

    def get_wf(self) -> dict:
        return self.req("GET", f"/api/workflows/{self.current_wf()}")

    def put_graph(self, wf_id: int, graph: dict) -> None:
        self.req("PUT", f"/api/workflows/{wf_id}", json={"graph": graph})


def cmd_login(args):
    server = args.server.rstrip("/")
    r = httpx.post(f"{server}/api/auth/login", json={"username": args.username}, timeout=10)
    if r.status_code >= 400:
        die(f"登录失败: HTTP {r.status_code} {r.text[:200]}")
    state = load_state()
    state.update(server=server, cookie=r.cookies.get("gf_session"))
    save_state(state)
    print(f"已登录 {args.username} @ {server}")


def cmd_st(args):
    cli = Cli()
    me = cli.req("GET", "/api/me")
    line = f"服务器 {cli.state['server']}  用户 {me['username']}"
    wf_id = cli.state.get("workflow_id")
    if wf_id:
        wf = cli.req("GET", f"/api/workflows/{wf_id}")
        line += f"  当前工作流 {wf['name']}（#{wf_id}）"
    print(line)


def cmd_wf_ls(args):
    cli = Cli()
    for w in cli.req("GET", "/api/workflows"):
        print(f"{w['id']:>4}  {w['name']}  {w['updated_at'][:19]}")


def cmd_wf_add(args):
    cli = Cli()
    w = cli.req("POST", "/api/workflows", json={"name": args.name})
    print(f"已创建工作流 {w['name']}（#{w['id']}）")


def cmd_wf_rm(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    cli.req("DELETE", f"/api/workflows/{wf_id}")
    print(f"已删除工作流 #{wf_id}")


def cmd_use(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    wf = cli.req("GET", f"/api/workflows/{wf_id}")
    cli.state["workflow_id"] = wf_id
    save_state(cli.state)
    print(f"当前工作流: {wf['name']}（#{wf_id}）")


def summarize(n: dict) -> str:
    c = n["config"]
    if n["type"] == "input":
        return f"数据集 {c.get('dataset_ids', [])}"
    if n["type"] == "llm_synth":
        return f"模型 #{c.get('model_config_id', '?')} -> {c.get('output_column', 'output')}"
    if n["type"] == "auto_process":
        return f"{len(c.get('operations', []))} 个操作"
    return f"保存为数据集「{c['dataset_name']}」" if c.get("save_as_dataset") else ""


def cmd_show(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    print(f"工作流 {wf['name']}（#{wf['id']}）")
    print(f"节点（{len(graph['nodes'])}）:")
    for n in graph["nodes"]:
        print(f"  {n['id']}  [{NODE_LABELS[n['type']]}]  {summarize(n)}")
    print(f"连线（{len(graph['edges'])}）:")
    for e in graph["edges"]:
        print(f"  {e['source']} -> {e['target']}")


LLM_CONFIG_KEYS = {"system": "system_prompt", "prompt": "user_prompt", "out": "output_column",
                   "mode": "output_mode", "fanout": "fanout_n", "conc": "concurrency",
                   "retries": "retries"}
LLM_PARAM_KEYS = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
                  "timeout": "timeout", "json_mode": "json_mode"}
INT_KEYS = {"fanout_n", "concurrency", "retries", "max_tokens", "timeout"}
FLOAT_KEYS = {"temperature", "top_p"}


def convert(field: str, v: str):
    if field in INT_KEYS:
        return int(v)
    if field in FLOAT_KEYS:
        return float(v)
    if field == "json_mode":
        return v.lower() in ("true", "1", "yes")
    return v


def parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"参数格式应为 key=value: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    die(f"节点 {node_id} 不存在")


def cmd_node_add(args):
    cli = Cli()
    ntype = NODE_TYPES.get(args.type)
    if ntype is None:
        die(f"未知节点类型 {args.type}（可选: input/llm/auto/output）")
    wf = cli.get_wf()
    nodes = wf["graph"]["nodes"]
    if args.id:
        node_id = args.id
        if any(n["id"] == node_id for n in nodes):
            die(f"节点 {node_id} 已存在")
    else:
        i = 1
        while any(n["id"] == f"{ntype}_{i}" for n in nodes):
            i += 1
        node_id = f"{ntype}_{i}"
    nodes.append({"id": node_id, "type": ntype,
                  "position": {"x": 80 + len(nodes) * 50, "y": 80 + len(nodes) * 40},
                  "config": {}})
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加节点 {node_id}")


def cmd_node_set(args):
    cli = Cli()
    wf = cli.get_wf()
    node = find_node(wf["graph"], args.id)
    cfg = node["config"]
    for k, v in parse_kv(args.pairs).items():
        if k == "dataset":
            cfg["dataset_ids"] = [cli.resolve("datasets", r) for r in v.split(",") if r]
        elif k == "model":
            cfg["model_config_id"] = cli.resolve("models", v)
        elif k == "save_as":
            cfg["save_as_dataset"] = bool(v)
            cfg["dataset_name"] = v
        elif k in LLM_CONFIG_KEYS:
            cfg[LLM_CONFIG_KEYS[k]] = convert(LLM_CONFIG_KEYS[k], v)
        elif k in LLM_PARAM_KEYS:
            cfg.setdefault("params", {})[LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        else:
            die(f"未知配置键 {k}")
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已更新 {args.id}: {json.dumps(cfg, ensure_ascii=False)}")


def cmd_node_show(args):
    cli = Cli()
    node = find_node(cli.get_wf()["graph"], args.id)
    print(json.dumps(node, ensure_ascii=False, indent=2))


def cmd_node_rm(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    find_node(graph, args.id)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != args.id]
    graph["edges"] = [e for e in graph["edges"] if args.id not in (e["source"], e["target"])]
    cli.put_graph(wf["id"], graph)
    print(f"已删除节点 {args.id} 及其连线")


def cmd_link(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    find_node(graph, args.source)
    find_node(graph, args.target)
    if any(e["source"] == args.source and e["target"] == args.target for e in graph["edges"]):
        die("连线已存在")
    graph["edges"].append({"source": args.source, "target": args.target, "kind": "normal"})
    cli.put_graph(wf["id"], graph)
    print(f"已连线 {args.source} -> {args.target}")


def cmd_unlink(args):
    cli = Cli()
    wf = cli.get_wf()
    graph = wf["graph"]
    before = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"]
                      if not (e["source"] == args.source and e["target"] == args.target)]
    if len(graph["edges"]) == before:
        die(f"不存在连线 {args.source} -> {args.target}")
    cli.put_graph(wf["id"], graph)
    print(f"已断开 {args.source} -> {args.target}")


OP_LABELS = {"dedup": "去重", "filter": "过滤", "rename": "重命名", "drop": "删除列",
             "concat": "拼接列", "cast": "类型转换", "sample": "随机采样", "shuffle": "打乱"}


def build_op(op: str, params: list[str]) -> dict:
    if op == "dedup":
        return {"op": "dedup", "columns": params[0].split(",") if params else []}
    if op == "filter":
        if len(params) != 3:
            die("用法: gf op add <节点> filter <列> <min_len|max_len|contains|not_contains|regex> <值>")
        col, mode, value = params
        return {"op": "filter", "column": col, "mode": mode,
                "value": int(value) if mode in ("min_len", "max_len") else value}
    if op == "rename":
        if len(params) != 2:
            die("用法: gf op add <节点> rename <原列> <新列>")
        return {"op": "rename", "mapping": {params[0]: params[1]}}
    if op == "drop":
        if len(params) != 1:
            die("用法: gf op add <节点> drop <列1,列2>")
        return {"op": "drop", "columns": params[0].split(",")}
    if op == "concat":
        if len(params) < 2:
            die("用法: gf op add <节点> concat <列1,列2> <目标列> [分隔符]")
        return {"op": "concat", "columns": params[0].split(","), "target": params[1],
                "sep": params[2] if len(params) > 2 else ""}
    if op == "cast":
        if len(params) != 2 or params[1] not in ("str", "int", "float"):
            die("用法: gf op add <节点> cast <列> <str|int|float>")
        return {"op": "cast", "column": params[0], "to": params[1]}
    if op == "sample":
        if len(params) != 1:
            die("用法: gf op add <节点> sample <n>")
        return {"op": "sample", "n": int(params[0])}
    if op == "shuffle":
        return {"op": "shuffle"}
    die(f"未知操作 {op}（可选: dedup/filter/rename/drop/concat/cast/sample/shuffle）")


def _auto_node(cli: Cli, node_id: str) -> tuple[dict, list]:
    wf = cli.get_wf()
    node = find_node(wf["graph"], node_id)
    if node["type"] != "auto_process":
        die(f"{node_id} 不是自动处理节点")
    return wf, node["config"].setdefault("operations", [])


def cmd_op_add(args):
    cli = Cli()
    wf, ops = _auto_node(cli, args.node_id)
    ops.append(build_op(args.op, args.params))
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加操作 #{len(ops)}: {json.dumps(ops[-1], ensure_ascii=False)}")


def cmd_op_ls(args):
    cli = Cli()
    _, ops = _auto_node(cli, args.node_id)
    for i, o in enumerate(ops, 1):
        rest = {k: v for k, v in o.items() if k != "op"}
        print(f"{i}. {OP_LABELS[o['op']]} {json.dumps(rest, ensure_ascii=False)}")


def cmd_op_rm(args):
    cli = Cli()
    wf, ops = _auto_node(cli, args.node_id)
    if not 1 <= args.index <= len(ops):
        die(f"序号超出范围（1-{len(ops)}）")
    removed = ops.pop(args.index - 1)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已删除操作: {OP_LABELS[removed['op']]}")


MODEL_KEYS = {"name": "name", "model": "model_name", "url": "base_url", "key": "api_key"}


def cmd_model_ls(args):
    cli = Cli()
    for m in cli.req("GET", "/api/models"):
        key = "已配置" if m["api_key_set"] else "未配置"
        print(f"{m['id']:>4}  {m['name']}  {m['model_name']}  {m['base_url']}  key:{key}")


def cmd_model_add(args):
    cli = Cli()
    m = cli.req("POST", "/api/models", json={
        "name": args.name, "model_name": args.model, "base_url": args.url,
        "api_key": args.key, "default_params": {}})
    print(f"已创建模型配置 {m['name']}（#{m['id']}）")


def cmd_model_set(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    hits = [m for m in cli.req("GET", "/api/models") if m["id"] == mc_id]
    if not hits:
        die("模型配置不存在")
    cur = hits[0]
    body = {"name": cur["name"], "model_name": cur["model_name"], "base_url": cur["base_url"],
            "api_key": "", "default_params": cur["default_params"]}  # key 留空=不修改
    for k, v in parse_kv(args.pairs).items():
        if k in MODEL_KEYS:
            body[MODEL_KEYS[k]] = v
        elif k in LLM_PARAM_KEYS:
            body["default_params"][LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        else:
            die(f"未知配置键 {k}")
    cli.req("PUT", f"/api/models/{mc_id}", json=body)
    print("已更新")


def cmd_model_rm(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    cli.req("DELETE", f"/api/models/{mc_id}")
    print(f"已删除模型配置 #{mc_id}")


def cmd_model_test(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    r = cli.req("POST", f"/api/models/{mc_id}/test")
    if r["ok"]:
        print(f"连通正常: {r['reply']}")
    else:
        die(f"连接失败: {r['error']}")


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(prog="gf", description="GraphFlow 命令行客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="登录")
    s.add_argument("username")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("st", help="当前状态")
    s.set_defaults(func=cmd_st)

    wf = sub.add_parser("wf", help="工作流管理").add_subparsers(dest="action", required=True)
    s = wf.add_parser("ls"); s.set_defaults(func=cmd_wf_ls)
    s = wf.add_parser("add"); s.add_argument("name"); s.set_defaults(func=cmd_wf_add)
    s = wf.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_wf_rm)

    s = sub.add_parser("use", help="设当前工作流")
    s.add_argument("ref")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("show", help="查看当前工作流图")
    s.set_defaults(func=cmd_show)

    node = sub.add_parser("node", help="节点管理").add_subparsers(dest="action", required=True)
    s = node.add_parser("add"); s.add_argument("type"); s.add_argument("id", nargs="?"); s.set_defaults(func=cmd_node_add)
    s = node.add_parser("set"); s.add_argument("id"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_node_set)
    s = node.add_parser("show"); s.add_argument("id"); s.set_defaults(func=cmd_node_show)
    s = node.add_parser("rm"); s.add_argument("id"); s.set_defaults(func=cmd_node_rm)

    s = sub.add_parser("link", help="连线")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="断开连线")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(func=cmd_unlink)

    op = sub.add_parser("op", help="自动处理操作").add_subparsers(dest="action", required=True)
    s = op.add_parser("add"); s.add_argument("node_id"); s.add_argument("op"); s.add_argument("params", nargs="*"); s.set_defaults(func=cmd_op_add)
    s = op.add_parser("ls"); s.add_argument("node_id"); s.set_defaults(func=cmd_op_ls)
    s = op.add_parser("rm"); s.add_argument("node_id"); s.add_argument("index", type=int); s.set_defaults(func=cmd_op_rm)

    model = sub.add_parser("model", help="模型配置").add_subparsers(dest="action", required=True)
    s = model.add_parser("ls"); s.set_defaults(func=cmd_model_ls)
    s = model.add_parser("add"); s.add_argument("name"); s.add_argument("--url", required=True); s.add_argument("--model", required=True); s.add_argument("--key", default=""); s.set_defaults(func=cmd_model_add)
    s = model.add_parser("set"); s.add_argument("ref"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_model_set)
    s = model.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_model_rm)
    s = model.add_parser("test"); s.add_argument("ref"); s.set_defaults(func=cmd_model_test)

    args = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # 启用 conhost 的 ANSI 转义支持（watch 进度刷新用）
    try:
        args.func(args)
    except httpx.ConnectError:
        die("无法连接服务器，请确认 GraphFlow 已启动")
    except ValueError as e:  # 如 conc=8.5 之类的数值转换错误
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
