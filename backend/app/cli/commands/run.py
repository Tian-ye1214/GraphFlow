"""运行：run / runs / watch / cancel / rerun / export / rows / logs。"""
import json
from pathlib import Path

from app.cli.client import Cli, die, watch_run, STATUS_LABELS


def cmd_run(args):
    cli = Cli()
    r = cli.req("POST", "/api/runs", json={"workflow_id": cli.current_wf()})
    print(f"运行 #{r['id']} 已启动")
    if args.follow:
        watch_run(cli, r["id"])


def cmd_runs(args):
    cli = Cli()
    for r in cli.req("GET", "/api/runs"):
        print(f"{r['id']:>4}  {r['workflow_name']}  "
              f"{STATUS_LABELS.get(r['status'], r['status'])}  {r['created_at'][:19]}")


def cmd_watch(args):
    cli = Cli()
    run_id = args.run_id
    if run_id is None:
        runs = cli.req("GET", "/api/runs", params={"workflow_id": cli.current_wf()})
        if not runs:
            die("当前工作流还没有运行记录")
        run_id = runs[0]["id"]
    watch_run(cli, run_id)


def cmd_cancel(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/cancel")
    print(f"已请求取消运行 #{args.run_id}")


def cmd_rerun(args):
    cli = Cli()
    cli.req("POST", f"/api/runs/{args.run_id}/rerun-failed")
    print(f"运行 #{args.run_id} 失败行已重新排队")


def cmd_export(args):
    cli = Cli()
    params = {"format": args.format}
    if args.node:
        params["node_id"] = args.node
    name = f"run{args.run_id}_{args.node}.{args.format}" if args.node else f"run{args.run_id}.{args.format}"
    out = Path(args.output or name)
    n = cli.download(f"/api/runs/{args.run_id}/export", out, params=params)
    print(f"已导出 {out}（{n} 字节）")


def _default_output_node(cli: Cli, run_id: int) -> str:
    d = cli.req("GET", f"/api/runs/{run_id}")
    outs = [n for n in d["graph"]["nodes"] if n["type"] == "output"]
    if not outs:
        die("该运行的工作流没有输出节点，请用 --node 指定")
    return outs[0]["id"]


def cmd_rows(args):
    cli = Cli()
    node = args.node or _default_output_node(cli, args.run_id)
    status = "failed" if args.failed else "done"
    page = cli.req("GET", f"/api/runs/{args.run_id}/rows",
                   params={"node_id": node, "status": status, "page": args.page, "page_size": 20})
    print(f"运行 #{args.run_id} 节点 {node} {status} 行（共 {page['total']}，第 {args.page} 页）")
    for row in page["rows"]:
        print(json.dumps(row, ensure_ascii=False))


def _print_model_log(m: dict, with_run: bool = False) -> None:
    head = f"[{m['source']}] {m['node_id'] or '-'}  {m['model_name']}"
    if with_run:
        head += f"  run#{m.get('run_id') or '-'}"
    head += f"  ({m['prompt_tokens']}+{m['completion_tokens']} tokens)"
    print(head)
    print(f"  请求: {json.dumps(m['request'], ensure_ascii=False)}")
    print(f"  回复: {m['response']}")


def cmd_logs(args):
    cli = Cli()
    if args.model:
        for m in cli.req("GET", f"/api/runs/{args.run_id}/model-logs"):
            _print_model_log(m)
    else:
        for l in cli.req("GET", f"/api/runs/{args.run_id}/logs"):
            print(f"[{l['created_at'][:19]}] {l['level'].upper()} {l['node_id'] or '-'}  {l['message']}")


def cmd_model_logs(args):
    cli = Cli()
    params = {"limit": args.limit}
    if args.source:
        params["source"] = args.source
    if args.run:
        params["run_id"] = args.run
    if args.node:
        params["node_id"] = args.node
    for m in cli.req("GET", "/api/model-logs", params=params):
        _print_model_log(m, with_run=True)


def cmd_qc(args):
    cli = Cli()
    if args.download:
        out = Path(args.output or f"run{args.run_id}_qc_failures.jsonl")
        n = cli.download(f"/api/runs/{args.run_id}/qc-failures.jsonl", out)
        print(f"已下载失败样本 {out}（{n} 字节）")
        return
    for m in cli.req("GET", f"/api/runs/{args.run_id}/qc-metrics"):
        print(f"{m['node_id']}  首轮通过 {m['first_round_pass']}/{m['total']}"
              f"（{round(m['first_round_rate'] * 100)}%）")
    fails = cli.req("GET", f"/api/runs/{args.run_id}/qc-failures")
    print(f"失败样本（{len(fails)}）:")
    for f in fails:
        reasons = "；".join(f"{r.get('status', '')}:{r['reason']}" for r in f["reasons"])
        print(f"  {json.dumps(f['sample'], ensure_ascii=False)}  -> {reasons}")


def cmd_rmrun(args):
    import sys
    if not args.all and args.run_id is None:
        print("用法: gf rmrun <run_id> | --all", file=sys.stderr)
        sys.exit(2)
    cli = Cli()
    if args.all:
        r = cli.req("DELETE", "/api/runs")
        print(f"已清空 {r['deleted']} 次运行")
    else:
        cli.req("DELETE", f"/api/runs/{args.run_id}")
        print(f"已删除运行 #{args.run_id}")


def register(sub):
    s = sub.add_parser("run", help="运行当前工作流")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("runs", help="运行列表")
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("watch", help="跟随运行进度")
    s.add_argument("run_id", nargs="?", type=int)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("cancel", help="取消运行")
    s.add_argument("run_id", type=int)
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("rerun", help="重跑失败行")
    s.add_argument("run_id", type=int)
    s.set_defaults(func=cmd_rerun)

    s = sub.add_parser("export", help="导出运行结果")
    s.add_argument("run_id", type=int)
    s.add_argument("-o", "--output")
    s.add_argument("--format", default="jsonl", choices=["jsonl", "csv", "xlsx"])
    s.add_argument("--node")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("rows", help="看运行某节点的结果行")
    s.add_argument("run_id", type=int)
    s.add_argument("--node"); s.add_argument("--failed", action="store_true")
    s.add_argument("--page", type=int, default=1)
    s.set_defaults(func=cmd_rows)

    s = sub.add_parser("logs", help="看运行日志（--model 看模型对话）")
    s.add_argument("run_id", type=int); s.add_argument("--model", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("model-logs", help="看模型调用日志(跨运行，含请求/回复/tokens)")
    s.add_argument("--source"); s.add_argument("--run", type=int); s.add_argument("--node")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_model_logs)

    s = sub.add_parser("qc", help="看质检指标+失败样本（--download 落 jsonl）")
    s.add_argument("run_id", type=int)
    s.add_argument("--download", action="store_true"); s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_qc)

    s = sub.add_parser("rmrun", help="删运行（给 ID 删单次，--all 清空全部）")
    s.add_argument("run_id", type=int, nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_rmrun)
