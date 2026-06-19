"""运行：run / runs / watch / cancel / rerun / export。"""
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
    r = cli.check(cli.http.get(f"/api/runs/{args.run_id}/export", params=params))
    name = f"run{args.run_id}_{args.node}.{args.format}" if args.node else f"run{args.run_id}.{args.format}"
    out = Path(args.output or name)
    out.write_bytes(r.content)
    print(f"已导出 {out}（{len(r.content)} 字节）")


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
