"""gf —— GraphFlow 命令行客户端（包）。所有操作通过 HTTP API 完成，与前端同权限。"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

STATE_FILE = Path(os.environ.get("GF_STATE_FILE") or Path.home() / ".graphflow" / "cli.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None):
    from app.cli.client import die
    from app.cli.commands import auth, workflow, node, model, dataset, prompt, run, agent
    p = argparse.ArgumentParser(prog="gf", description="GraphFlow 命令行客户端")
    sub = p.add_subparsers(dest="cmd", required=True)
    for mod in (auth, workflow, node, model, dataset, prompt, run, agent):
        mod.register(sub)
    args = p.parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # 启用 conhost ANSI（watch 进度刷新用）
    try:
        args.func(args)
    except httpx.ConnectError:
        die("无法连接服务器，请确认 GraphFlow 已启动")
    except ValueError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
