"""工作流与图结构：wf ls|add|rm|restore|rename|export|import / use / show / cols / link / unlink / node add|rm。"""
import re
from pathlib import Path

from app.cli import save_state
from app.cli.client import Cli, die, summarize, NODE_LABELS
from app.cli.commands.node import node_actions
from app.services import graph_ops


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
    wf = cli.get_wf()
    node_id = graph_ops.add_node(wf["graph"], args.type, args.id)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加节点 {node_id}")


def cmd_node_rm(args):
    cli = Cli()
    wf = cli.get_wf()
    graph_ops.remove_node(wf["graph"], args.id)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已删除节点 {args.id} 及其连线")


def cmd_link(args):
    cli = Cli()
    wf = cli.get_wf()
    graph_ops.connect(wf["graph"], args.source, args.target, args.kind)
    cli.put_graph(wf["id"], wf["graph"])
    arrow = "⟲回扫" if args.kind == "rescan" else "->"
    print(f"已连线 {args.source} {arrow} {args.target}")


def cmd_unlink(args):
    cli = Cli()
    wf = cli.get_wf()
    graph_ops.disconnect(wf["graph"], args.source, args.target)
    cli.put_graph(wf["id"], wf["graph"])
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


def cmd_wf_export(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref) if args.ref else cli.current_wf()
    wf = cli.req("GET", f"/api/workflows/{wf_id}")
    if args.output:
        out = Path(args.output)
    else:   # 默认名取自服务端链路名：仅留末段并替非法字符，避免名含 / 写错位置
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(wf["name"]).name).strip(" .") or "workflow"
        out = Path(f"{safe}.gfpkg")
    cli.download(f"/api/workflows/{wf_id}/export", out)
    print(f"已导出链路「{wf['name']}」到 {out}")


def cmd_wf_import(args):
    cli = Cli()
    path = Path(args.file)
    if not path.is_file():
        die(f"文件不存在: {args.file}")
    with open(path, "rb") as f:
        d = cli.req("POST", "/api/workflows/import",
                    files={"file": (path.name, f, "application/zip")})
    rep, w = d["report"], d["workflow"]
    print(f"已导入为链路「{w['name']}」(#{w['id']})")
    reused = ([f"模型 {x['name']}" for x in rep["models_reused"]]
              + [f"提示词 {x['name']}" for x in rep["prompts_reused"]]
              + [f"数据集 {x['name']}" for x in rep["datasets_reused"]])
    if reused:
        print("  复用: " + "、".join(reused))
    if rep["models_need_key"]:
        print("  待回填密钥的模型: " + "、".join(x["name"] for x in rep["models_need_key"]))
    if rep["secrets_need_refill"]:
        print("  待回填的密钥位: " + "、".join(
            f"{x['node_id']}.{x['field']}" for x in rep["secrets_need_refill"]))
    if rep["draft_unresolved"]:
        print("  ⚠ 有引用无法解析，已降级草稿: " + "、".join(
            f"{x['node_id']}({x['kind']})" for x in rep["draft_unresolved"]))


def register(sub):
    wf = sub.add_parser("wf", help="工作流管理").add_subparsers(dest="action", required=True)
    s = wf.add_parser("ls"); s.set_defaults(func=cmd_wf_ls)
    s = wf.add_parser("add"); s.add_argument("name"); s.set_defaults(func=cmd_wf_add)
    s = wf.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_wf_rm)
    s = wf.add_parser("restore"); s.add_argument("run_id", type=int); s.set_defaults(func=cmd_wf_restore)
    s = wf.add_parser("rename"); s.add_argument("ref"); s.add_argument("name"); s.set_defaults(func=cmd_wf_rename)
    s = wf.add_parser("export", help="导出链路为 .gfpkg 包")
    s.add_argument("ref", nargs="?"); s.add_argument("-o", "--output"); s.set_defaults(func=cmd_wf_export)
    s = wf.add_parser("import", help="从 .gfpkg 包导入链路")
    s.add_argument("file"); s.set_defaults(func=cmd_wf_import)

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
