"""工作流与图结构：wf ls|add|rm|restore|rename|dump|load / use / show / cols / link / unlink / node add|rm。"""
import json
from pathlib import Path

from app.cli import save_state
from app.cli.client import Cli, die, find_node, summarize, NODE_TYPES, NODE_LABELS
from app.cli.commands.node import node_actions


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


def cmd_wf_restore(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/restore")
    print(f"已从运行 #{args.run_id} 的版本恢复工作流图")


def cmd_use(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    wf = cli.req("GET", f"/api/workflows/{wf_id}")
    cli.state["workflow_id"] = wf_id
    save_state(cli.state)
    print(f"当前工作流: {wf['name']}（#{wf_id}）")


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
        arrow = "⟲回扫" if e.get("kind") == "rescan" else "->"
        print(f"  {e['source']} {arrow} {e['target']}")


def cmd_node_add(args):
    cli = Cli()
    ntype = NODE_TYPES.get(args.type)
    if ntype is None:
        die(f"未知节点类型 {args.type}（可选: input/llm/auto/output/qc/http）")
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
    src = find_node(graph, args.source)
    find_node(graph, args.target)
    if args.kind == "rescan" and src["type"] != "qc":
        die("rescan 回扫边必须从 qc 节点出发")
    if any(e["source"] == args.source and e["target"] == args.target for e in graph["edges"]):
        die("连线已存在")
    graph["edges"].append({"source": args.source, "target": args.target, "kind": args.kind})
    cli.put_graph(wf["id"], graph)
    arrow = "⟲回扫" if args.kind == "rescan" else "->"
    print(f"已连线 {args.source} {arrow} {args.target}")


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


def cmd_wf_rename(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref)
    cli.req("PUT", f"/api/workflows/{wf_id}", json={"name": args.name})
    print(f"已重命名工作流 #{wf_id} -> {args.name}")


def cmd_cols(args):
    cli = Cli()
    wf_id = cli.current_wf()
    cols = cli.req("GET", f"/api/workflows/{wf_id}/columns")
    if args.node and args.node not in cols:
        die(f"节点 {args.node} 不存在")
    items = {args.node: cols[args.node]} if args.node else cols
    for nid, io in items.items():
        print(f"{nid}")
        print(f"  输入: {', '.join(io['input']) or '（无）'}")
        print(f"  输出: {', '.join(io['output']) or '（无）'}")


def cmd_wf_dump(args):
    cli = Cli()
    wf = cli.get_wf()
    out = Path(args.output or f"{wf['name']}.json")
    out.write_text(json.dumps(wf["graph"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出工作流图到 {out}")


def cmd_wf_load(args):
    cli = Cli()
    path = Path(args.file)
    if not path.is_file():
        die(f"文件不存在: {args.file}")
    graph = json.loads(path.read_text(encoding="utf-8"))
    wf_id = cli.current_wf()
    cli.put_graph(wf_id, graph)
    print(f"已从 {args.file} 载入工作流图（#{wf_id}）")


def register(sub):
    wf = sub.add_parser("wf", help="工作流管理").add_subparsers(dest="action", required=True)
    s = wf.add_parser("ls"); s.set_defaults(func=cmd_wf_ls)
    s = wf.add_parser("add"); s.add_argument("name"); s.set_defaults(func=cmd_wf_add)
    s = wf.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_wf_rm)
    s = wf.add_parser("restore"); s.add_argument("run_id", type=int); s.set_defaults(func=cmd_wf_restore)
    s = wf.add_parser("rename"); s.add_argument("ref"); s.add_argument("name"); s.set_defaults(func=cmd_wf_rename)
    s = wf.add_parser("dump"); s.add_argument("-o", "--output"); s.set_defaults(func=cmd_wf_dump)
    s = wf.add_parser("load"); s.add_argument("file"); s.set_defaults(func=cmd_wf_load)

    s = sub.add_parser("use", help="设当前工作流")
    s.add_argument("ref")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("show", help="查看当前工作流图")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("cols", help="列血缘（各节点输入/输出列）")
    s.add_argument("node", nargs="?")
    s.set_defaults(func=cmd_cols)

    node = node_actions(sub)
    s = node.add_parser("add"); s.add_argument("type"); s.add_argument("id", nargs="?"); s.set_defaults(func=cmd_node_add)
    s = node.add_parser("rm"); s.add_argument("id"); s.set_defaults(func=cmd_node_rm)

    s = sub.add_parser("link", help="连线")
    s.add_argument("source"); s.add_argument("target")
    s.add_argument("--kind", choices=["normal", "rescan"], default="normal", help="rescan=质检回扫边")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="断开连线")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(func=cmd_unlink)
