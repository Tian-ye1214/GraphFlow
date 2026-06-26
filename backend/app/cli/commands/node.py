"""节点配置与自动处理操作：node set / node show / node prompt / op add|ls|rm。"""
import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path

from app.cli.client import Cli, die, parse_kv
from app.services import graph_ops


def node_actions(sub):
    """创建（或复用）`node` 子命令的 action 子解析器。

    `node` 的动作分散在 workflow.py（add/rm）与本模块（set/show），两边须共用
    同一个 action 子解析器对象，否则 argparse 会因重名 `node` 冲突报错。
    通过缓存到 `sub` 上保证只创建一次。"""
    cached = getattr(sub, "_gf_node_actions", None)
    if cached is None:
        cached = sub.add_parser("node", help="节点管理").add_subparsers(dest="action", required=True)
        sub._gf_node_actions = cached
    return cached


def cmd_node_set(args):
    cli = Cli()
    wf = cli.get_wf()
    node = graph_ops.find_node(wf["graph"], args.id)
    for k, v in parse_kv(args.pairs).items():
        if k in graph_ops.RESOLVE_KEYS:                 # dataset/model/judge_models：名/ID→id
            kind, is_list = graph_ops.RESOLVE_KEYS[k]
            refs = [r for r in v.split(",") if r] if is_list else [v]
            ids = [cli.resolve(kind, r) for r in refs]
            graph_ops.apply_node_config(node, k, ids if is_list else ids[0])
        elif k in ("extract", "headers"):               # 冒号串→dict（缺冒号优雅 die）
            fmt = "列:JSON路径" if k == "extract" else "名:值"
            graph_ops.apply_node_config(node, k, _parse_colon_map(v, k, fmt))
        else:
            graph_ops.apply_node_config(node, k, v)     # 其余键 + 类型转换走单点
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已更新 {args.id}: {json.dumps(node['config'], ensure_ascii=False)}")


def cmd_node_show(args):
    cli = Cli()
    node = graph_ops.find_node(cli.get_wf()["graph"], args.id)
    print(json.dumps(node, ensure_ascii=False, indent=2))


def _parse_colon_map(v: str, key: str, fmt: str) -> dict:
    """CLI 侧解析 `a:b,c:d`：复用 graph_ops 单点逻辑，把 GraphOpError 转成命令行 die（SystemExit）。"""
    try:
        return graph_ops._parse_colon_map(v, key, fmt)
    except graph_ops.GraphOpError as e:
        die(str(e))


def _read_prompt(args) -> str:
    if args.file:
        p = Path(args.file)
        if not p.is_file():   # 对齐 data up：文件不存在优雅 die，不裸 FileNotFoundError
            die(f"文件不存在: {args.file}")
        return p.read_text(encoding="utf-8")
    if args.edit:
        editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "vi")
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
            tmp = f.name
        try:
            subprocess.call([editor, tmp])
        except OSError:   # EDITOR 指向不存在程序：优雅 die，不裸 FileNotFoundError
            die(f"无法启动编辑器: {editor}")
        return Path(tmp).read_text(encoding="utf-8")
    return sys.stdin.read()   # args.from_stdin


def add_prompt_source_args(parser, *, required=True):
    """添加 `--file/--edit/-`(stdin) 互斥提示词来源参数组，返回该组（供调用方追加额外互斥项）。
    与 _read_prompt 配对使用。"""
    g = parser.add_mutually_exclusive_group(required=required)
    g.add_argument("--file")
    g.add_argument("--edit", action="store_true")
    g.add_argument("-", dest="from_stdin", action="store_true")
    return g


def cmd_node_prompt(args):
    cli = Cli()
    wf = cli.get_wf()
    node = graph_ops.find_node(wf["graph"], args.id)
    field = "system_prompt" if args.system else "user_prompt"
    cfg = node["config"]
    if args.library:
        pid = cli.resolve("prompts", args.library)
        if args.ref:
            cfg[f"{field}_ref"] = pid
            msg = f"已将 {args.id} 的 {field} 设为引用提示词 #{pid}（运行时取最新版）"
        else:   # 默认 copy：拉当前正文内联，并清除引用
            body = cli.req("GET", f"/api/prompts/{pid}")["current"]["body"]
            cfg[field] = body
            cfg.pop(f"{field}_ref", None)
            msg = f"已复制提示词 #{pid} 到 {args.id} 的 {field}（{len(body)} 字符）"
    else:
        cfg[field] = _read_prompt(args)
        cfg.pop(f"{field}_ref", None)   # 写内联即解除引用
        msg = f"已写入 {args.id} 的 {field}（{len(cfg[field])} 字符）"
    cli.put_graph(wf["id"], wf["graph"])
    print(msg)


def cmd_op_add(args):
    cli = Cli()
    wf = cli.get_wf()
    node = graph_ops.find_node(wf["graph"], args.node_id)
    op = graph_ops.add_op(node, args.op, args.params)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已添加操作 #{len(node['config']['operations'])}: {json.dumps(op, ensure_ascii=False)}")


def cmd_op_ls(args):
    cli = Cli()
    node = graph_ops.find_node(cli.get_wf()["graph"], args.node_id)
    if node["type"] != "auto_process":
        die(f"{args.node_id} 不是自动处理节点(auto_process)")   # 文案与 add_op/remove_op 一致
    for i, o in enumerate(node["config"].get("operations", []), 1):
        rest = {k: v for k, v in o.items() if k != "op"}
        print(f"{i}. {graph_ops.OP_LABELS[o['op']]} {json.dumps(rest, ensure_ascii=False)}")


def cmd_op_rm(args):
    cli = Cli()
    wf = cli.get_wf()
    node = graph_ops.find_node(wf["graph"], args.node_id)
    removed = graph_ops.remove_op(node, args.index)
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已删除操作: {graph_ops.OP_LABELS[removed['op']]}")


def register(sub):
    node = node_actions(sub)
    s = node.add_parser("set"); s.add_argument("id"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_node_set)
    s = node.add_parser("show"); s.add_argument("id"); s.set_defaults(func=cmd_node_show)

    s = node.add_parser("prompt")
    s.add_argument("id")
    g1 = s.add_mutually_exclusive_group(required=True)
    g1.add_argument("--system", action="store_true")
    g1.add_argument("--user", action="store_true")
    g2 = add_prompt_source_args(s)
    g2.add_argument("--library", help="库提示词 id 或名")
    g3 = s.add_mutually_exclusive_group()
    g3.add_argument("--ref", action="store_true", help="引用（运行时取最新版）")
    g3.add_argument("--copy", action="store_true", help="复制当前正文进来（默认）")
    s.set_defaults(func=cmd_node_prompt)

    op = sub.add_parser("op", help="自动处理操作").add_subparsers(dest="action", required=True)
    s = op.add_parser("add"); s.add_argument("node_id"); s.add_argument("op"); s.add_argument("params", nargs="*"); s.set_defaults(func=cmd_op_add)
    s = op.add_parser("ls"); s.add_argument("node_id"); s.set_defaults(func=cmd_op_ls)
    s = op.add_parser("rm"); s.add_argument("node_id"); s.add_argument("index", type=int); s.set_defaults(func=cmd_op_rm)
