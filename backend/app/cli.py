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

    args = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # 启用 conhost 的 ANSI 转义支持（watch 进度刷新用）
    try:
        args.func(args)
    except httpx.ConnectError:
        die("无法连接服务器，请确认 GraphFlow 已启动")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
