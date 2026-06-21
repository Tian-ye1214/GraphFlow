"""数据集：data ls|up|head|rm|download。"""
import json
from pathlib import Path

from app.cli.client import Cli, die


def cmd_data_ls(args):
    cli = Cli()
    for d in cli.req("GET", "/api/datasets"):
        meta = d.get("source", "") or "-"
        if d.get("original_filename"):
            meta += f" {d['original_filename']}"
        print(f"{d['id']:>4}  {d['name']}  {d['row_count']} 行  [{','.join(d['columns'])}]  "
              f"{meta}  {(d.get('created_at') or '')[:19]}")


def cmd_data_up(args):
    cli = Cli()
    files = []
    for p in args.files:
        path = Path(p)
        if not path.is_file():
            die(f"文件不存在: {p}")
        files.append(("files", (path.name, path.read_bytes())))
    for d in cli.req("POST", "/api/datasets/upload", files=files):
        print(f"已上传 {d['name']}（#{d['id']}，{d['row_count']} 行）")


def cmd_data_head(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    page = cli.req("GET", f"/api/datasets/{ds_id}/rows", params={"page": 1, "page_size": args.n})
    for row in page["rows"]:
        print(json.dumps(row, ensure_ascii=False))


def cmd_data_rm(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    cli.req("DELETE", f"/api/datasets/{ds_id}")
    print(f"已删除数据集 #{ds_id}")


def cmd_data_download(args):
    cli = Cli()
    ds_id = cli.resolve("datasets", args.ref)
    out = Path(args.output or f"{args.ref}.{args.format}")
    n = cli.download(f"/api/datasets/{ds_id}/export", out, params={"format": args.format})
    print(f"已下载 {out}（{n} 字节）")


def register(sub):
    data = sub.add_parser("data", help="数据集").add_subparsers(dest="action", required=True)
    s = data.add_parser("ls"); s.set_defaults(func=cmd_data_ls)
    s = data.add_parser("up"); s.add_argument("files", nargs="+"); s.set_defaults(func=cmd_data_up)
    s = data.add_parser("head"); s.add_argument("ref"); s.add_argument("n", nargs="?", type=int, default=5); s.set_defaults(func=cmd_data_head)
    s = data.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_data_rm)
    s = data.add_parser("download")
    s.add_argument("ref"); s.add_argument("-o", "--output")
    s.add_argument("--format", default="jsonl", choices=["jsonl", "csv", "xlsx"])
    s.set_defaults(func=cmd_data_download)
