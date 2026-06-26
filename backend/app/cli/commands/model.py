"""模型配置：model ls|add|set|rm|test。"""
from app.cli.client import Cli, die, parse_kv, MODEL_KEYS
from app.services.graph_ops import _convert as convert, LLM_PARAM_KEYS


def cmd_model_ls(args):
    cli = Cli()
    for m in cli.req("GET", "/api/models"):
        key = "已配置" if m["api_key_set"] else "未配置"
        provider = m.get("provider", "openai")
        api_version = m.get("api_version") or "-"
        print(f"{m['id']:>4}  {m['name']}  {m['model_name']}  {m['base_url']}  "
              f"provider:{provider}  api_version:{api_version}  key:{key}")


def cmd_model_add(args):
    cli = Cli()
    m = cli.req("POST", "/api/models", json={
        "name": args.name, "model_name": args.model, "base_url": args.url,
        "provider": args.provider, "api_version": args.api_version,
        "api_key": args.key, "default_params": {}})
    print(f"已创建模型配置 {m['name']}（#{m['id']}）")


def cmd_model_set(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    hits = [m for m in cli.req("GET", "/api/models") if m["id"] == mc_id]
    if not hits:
        die("模型配置不存在")
    cur = hits[0]
    body = {"name": cur["name"], "model_name": cur["model_name"], "base_url": cur["base_url"],
            "provider": cur.get("provider", "openai"), "api_version": cur.get("api_version", ""),
            "api_key": "", "default_params": cur["default_params"]}  # key 留空=不修改
    for k, v in parse_kv(args.pairs).items():
        if k in MODEL_KEYS:
            body[MODEL_KEYS[k]] = v
        elif k in LLM_PARAM_KEYS:
            body["default_params"][LLM_PARAM_KEYS[k]] = convert(LLM_PARAM_KEYS[k], v)
        else:
            die(f"未知配置键 {k}")
    cli.req("PUT", f"/api/models/{mc_id}", json=body)
    print("已更新")


def cmd_model_rm(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    cli.req("DELETE", f"/api/models/{mc_id}")
    print(f"已删除模型配置 #{mc_id}")


def cmd_model_test(args):
    cli = Cli()
    mc_id = cli.resolve("models", args.ref)
    r = cli.req("POST", f"/api/models/{mc_id}/test")
    if r["ok"]:
        print("连通正常")
    else:
        die(f"连接失败: {r['error']}")


def register(sub):
    model = sub.add_parser("model", help="模型配置").add_subparsers(dest="action", required=True)
    s = model.add_parser("ls"); s.set_defaults(func=cmd_model_ls)
    s = model.add_parser("add"); s.add_argument("name"); s.add_argument("--url", required=True); s.add_argument("--model", required=True); s.add_argument("--key", default=""); s.add_argument("--provider", choices=["openai", "azure"], default="openai"); s.add_argument("--api-version", default=""); s.set_defaults(func=cmd_model_add)
    s = model.add_parser("set"); s.add_argument("ref"); s.add_argument("pairs", nargs="+"); s.set_defaults(func=cmd_model_set)
    s = model.add_parser("rm"); s.add_argument("ref"); s.set_defaults(func=cmd_model_rm)
    s = model.add_parser("test"); s.add_argument("ref"); s.set_defaults(func=cmd_model_test)
