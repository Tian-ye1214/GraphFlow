"""提示词库：prompt ls / show / add / edit / rm / versions / rollback / dup。"""
from app.cli.client import Cli
from app.cli.commands.node import _read_prompt, add_prompt_source_args


def cmd_prompt_ls(args):
    cli = Cli()
    for p in cli.req("GET", "/api/prompts"):
        vs = "、".join(f"{{{{{v}}}}}" for v in p["variables"]) or "（无）"
        print(f"{p['id']:>4}  {p['name']}  v{p['latest_version']}  变量:{vs}  {p['description']}")


def cmd_prompt_show(args):
    cli = Cli()
    d = cli.req("GET", f"/api/prompts/{cli.resolve('prompts', args.ref)}")
    print(f"#{d['id']} {d['name']}（v{d['current']['version']}）  {d['description']}")
    print(f"变量: {'、'.join(d['current']['variables']) or '（无）'}")
    used = d.get("used_by", [])
    if used:
        print("被引用: " + "、".join(f"{u['workflow_name']}/{u['node_id']}({u['slot']})" for u in used))
    print("---\n" + d["current"]["body"])


def cmd_prompt_add(args):
    cli = Cli()
    d = cli.req("POST", "/api/prompts",
                json={"name": args.name, "description": args.desc or "", "body": _read_prompt(args)})
    print(f"已创建提示词 {d['name']}（#{d['id']}，v1，{len(d['current']['body'])} 字符）")


def cmd_prompt_edit(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    cur = cli.req("GET", f"/api/prompts/{pid}")
    payload = {"name": args.name or cur["name"],
               "description": args.desc if args.desc is not None else cur["description"],
               "body": _read_prompt(args)}
    d = cli.req("PUT", f"/api/prompts/{pid}", json=payload)
    print(f"已更新提示词 #{pid}（当前 v{d['current']['version']}）")


def cmd_prompt_rm(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    cli.req("DELETE", f"/api/prompts/{pid}")
    print(f"已删除提示词 #{pid}")


def cmd_prompt_versions(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    for v in cli.req("GET", f"/api/prompts/{pid}/versions"):
        head = v["body"].splitlines()[0] if v["body"] else ""
        print(f"v{v['version']}  {v['created_at'][:19]}  {head[:40]}")


def cmd_prompt_rollback(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    d = cli.req("POST", f"/api/prompts/{pid}/rollback", json={"version": args.version})
    print(f"已回滚提示词 #{pid} 到 v{args.version}（生成 v{d['current']['version']}）")


def cmd_prompt_dup(args):
    cli = Cli()
    pid = cli.resolve("prompts", args.ref)
    body = {"name": args.name} if args.name else {}
    d = cli.req("POST", f"/api/prompts/{pid}/duplicate", json=body)
    print(f"已复制为新提示词 {d['name']}（#{d['id']}）")


def register(sub):
    prompt = sub.add_parser("prompt", help="提示词库").add_subparsers(dest="action", required=True)
    s = prompt.add_parser("ls"); s.set_defaults(func=cmd_prompt_ls)
    s = prompt.add_parser("show"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_show)

    s = prompt.add_parser("add")
    s.add_argument("name"); s.add_argument("--desc")
    add_prompt_source_args(s)
    s.set_defaults(func=cmd_prompt_add)

    s = prompt.add_parser("edit")
    s.add_argument("ref"); s.add_argument("--name"); s.add_argument("--desc")
    add_prompt_source_args(s)
    s.set_defaults(func=cmd_prompt_edit)

    s = prompt.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_rm)
    s = prompt.add_parser("versions"); s.add_argument("ref"); s.set_defaults(func=cmd_prompt_versions)
    s = prompt.add_parser("rollback"); s.add_argument("ref"); s.add_argument("version", type=int); s.set_defaults(func=cmd_prompt_rollback)
    s = prompt.add_parser("dup"); s.add_argument("ref"); s.add_argument("--name"); s.set_defaults(func=cmd_prompt_dup)
