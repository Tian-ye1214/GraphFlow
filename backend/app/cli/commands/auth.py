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
    cookie = r.cookies.get("gf_session")
    if not cookie:
        die("登录响应未携带会话 cookie，请检查服务器/反向代理")
    state = load_state()
    state.update(server=server, cookie=cookie)
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


def cmd_logout(args):
    cli = Cli()
    try:
        cli.req("POST", "/api/auth/logout")
    except SystemExit:
        pass  # 服务器端登出失败也要清本地状态
    state = load_state()
    state.pop("cookie", None)
    state.pop("workflow_id", None)
    save_state(state)
    print("已登出")


def register(sub):
    s = sub.add_parser("login", help="登录")
    s.add_argument("username")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("st", help="当前状态")
    s.set_defaults(func=cmd_st)

    s = sub.add_parser("logout", help="登出并清本地状态")
    s.set_defaults(func=cmd_logout)
