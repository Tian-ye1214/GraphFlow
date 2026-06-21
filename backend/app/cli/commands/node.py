"""节点配置与自动处理操作：node set / node show / node prompt / op add|ls|rm。"""
import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path

from app.cli.client import (Cli, die, find_node, parse_kv, convert, build_op,
                            _auto_node, LLM_CONFIG_KEYS, LLM_PARAM_KEYS, HTTP_STR_KEYS, OP_LABELS)


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
        elif k == "judge_models":
            cfg["judge_model_ids"] = [cli.resolve("models", r) for r in v.split(",") if r]
        elif k == "pass_k":
            cfg["pass_k"] = int(v)
        elif k == "max_rounds":
            cfg["max_rounds"] = int(v)
        elif k in HTTP_STR_KEYS:
            cfg[k] = v
        elif k == "extract":
            cfg["extract"] = _parse_colon_map(v, "extract", "列:JSON路径")
        elif k in LLM_CONFIG_KEYS:
            cfg[LLM_CONFIG_KEYS[k]] = convert(LLM_CONFIG_KEYS[k], v)
        elif k in LLM_PARAM_KEYS:
            cfg.setdefault("params", {})[LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        elif k == "drop":
            cfg["drop_columns"] = [c for c in v.split(",") if c]
        elif k == "outs":
            cfg["output_columns"] = [c for c in v.split(",") if c]
        elif k == "status_col":
            cfg["status_column"] = v
        elif k == "feedback_col":
            cfg["feedback_column"] = v
        elif k == "think":
            cfg.setdefault("params", {})["thinking_enabled"] = v.lower() in ("on", "true", "1", "yes")
        elif k == "effort":
            cfg.setdefault("params", {})["reasoning_effort"] = v
        elif k == "headers":
            cfg["headers"] = _parse_colon_map(v, "headers", "名:值")
        else:
            die(f"未知配置键 {k}")
    cli.put_graph(wf["id"], wf["graph"])
    print(f"已更新 {args.id}: {json.dumps(cfg, ensure_ascii=False)}")


def cmd_node_show(args):
    cli = Cli()
    node = find_node(cli.get_wf()["graph"], args.id)
    print(json.dumps(node, ensure_ascii=False, indent=2))


def _parse_colon_map(v: str, key: str, fmt: str) -> dict:
    """解析 `a:b,c:d` 形式（首个冒号切分，值可含冒号）。非空但缺冒号的段 → die，
    不把用户输入静默吞成空 dict（对齐 parse_kv/build_op 的 die+用法提示惯例）。"""
    out = {}
    for seg in v.split(","):
        if not seg.strip():
            continue   # 容忍尾随/多余逗号
        if ":" not in seg:
            die(f"{key} 格式应为 {fmt}[,{fmt}]，缺少冒号: {seg!r}")
        k, val = seg.split(":", 1)
        out[k] = val
    return out


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
    node = find_node(wf["graph"], args.id)
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


def cmd_node_try(args):
    cli = Cli()
    wf = cli.get_wf()
    find_node(wf["graph"], args.id)   # 本地校验节点存在，给清晰中文报错
    body = {"call_model": not args.render, "limit": args.limit,
            "allow_side_effects": args.allow_side_effects}
    res = cli.req("POST", f"/api/workflows/{wf['id']}/nodes/{args.id}/dry-run", json=body)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    _print_dry_run(res)


def _print_dry_run(res: dict) -> None:
    head = (f"试跑 {res['node_id']} ({res['node_type']}) — 样本来源 {res['sample_source']}"
            + (f" run#{res['run_id']}" if res.get("run_id") else "") + f"，{res['sampled']} 行")
    print(head)
    if res.get("note"):
        print(res["note"]); return
    if res.get("needs_confirm"):
        print(res.get("side_effect_note", "需确认副作用（加 --allow-side-effects 重试）")); return
    if res.get("error"):
        print(f"错误: {res['error']}")
    if "output_rows" in res:                       # auto_process
        for i, r in enumerate(res["output_rows"], 1):
            print(f"  [{i}] {json.dumps(r, ensure_ascii=False)}")
        print(f"产出列: {res.get('output_columns')}")
        return
    for i, row in enumerate(res.get("rows", []), 1):
        print(f"── 第 {i} 行 ──")
        if row.get("rendered_system"):
            print(f"  system: {row['rendered_system']}")
        if "rendered_url" in row:
            print(f"  {row['method']} {row['rendered_url']}")
        else:
            print(f"  user: {row.get('rendered_user', '')}")
        if row.get("missing_cols"):
            print(f"  ⚠ 缺失列: {row['missing_cols']}")
        if row.get("error"):
            print(f"  ✗ 错误: {row['error']}")
        elif "output" in row:
            print(f"  产出: {json.dumps(row['output'], ensure_ascii=False)}")
            if row.get("new_columns"):
                print(f"  新增列: {row['new_columns']}")
        elif row.get("passed") is not None:        # qc
            print(f"  判定: {'通过' if row['passed'] else '不通过'} — {row.get('reason', '')}")
    u = res.get("usage") or {}
    if u.get("prompt_tokens") or u.get("completion_tokens"):
        print(f"tokens: prompt={u['prompt_tokens']} completion={u['completion_tokens']}")


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


def register(sub):
    node = node_actions(sub)
    s = node.add_parser("set"); s.add_argument("id"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_node_set)
    s = node.add_parser("show"); s.add_argument("id"); s.set_defaults(func=cmd_node_show)

    s = node.add_parser("try", help="试跑节点（零副作用，不落库）")
    s.add_argument("id")
    s.add_argument("--render", action="store_true", help="只渲染提示词，不调模型（免费）")
    s.add_argument("--limit", type=int, default=3, help="试跑样本行数（模型节点上限 3）")
    s.add_argument("--allow-side-effects", dest="allow_side_effects", action="store_true",
                   help="http_fetch 允许非 GET/HEAD 方法")
    s.add_argument("--json", action="store_true", help="输出原始 JSON")
    s.set_defaults(func=cmd_node_try)

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
