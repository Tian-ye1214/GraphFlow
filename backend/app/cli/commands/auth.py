"""认证与会话：login / st。"""
import httpx

from app.cli import load_state, save_state
from app.cli.client import Cli, die


def cmd_login(args):
    server = args.server.rstrip("/")
    r = httpx.post(f"{server}/api/auth/login", json={"username": args.username},
                   timeout=10, trust_env=False)  # 同上：登录也不走系统代理
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


def register(sub):
    s = sub.add_parser("login", help="登录")
    s.add_argument("username")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("st", help="当前状态")
    s.set_defaults(func=cmd_st)
