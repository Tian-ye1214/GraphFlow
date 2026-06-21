"""RedLotus Agent 会话只读回看：agent ls|show。"""
import json

from app.cli.client import Cli


def cmd_agent_ls(args):
    cli = Cli()
    for s in cli.req("GET", "/api/agent/sessions"):
        print(f"{s['id']:>4}  {s['title']}  [{s['status']}]  {s['updated_at'][:19]}")


def cmd_agent_show(args):
    cli = Cli()
    d = cli.req("GET", f"/api/agent/sessions/{args.sid}")
    print(f"会话 #{d['id']}  {d['title']}  [{d['status']}]")
    for m in d["messages"]:
        c = m["content"]
        text = c.get("text") if isinstance(c, dict) else None
        if text is None:   # tool 消息等无 text 的内容：原样打 JSON
            text = json.dumps(c, ensure_ascii=False)
        print(f"  [{m['role']}] {text}")


def register(sub):
    agent = sub.add_parser("agent", help="RedLotus Agent 会话(只读回看)").add_subparsers(
        dest="action", required=True)
    s = agent.add_parser("ls", help="列出会话"); s.set_defaults(func=cmd_agent_ls)
    s = agent.add_parser("show", help="看会话消息流")
    s.add_argument("sid", type=int); s.set_defaults(func=cmd_agent_show)
